"""联合去标识包接口编排：发起 / 预检缺口 / 审批 / 重试 / 下载。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from src.models.user import User
from src.schemas.bundle import BundleCreateRequest, BundlePreviewRequest
from src.schemas.export import ReviewRequest
from src.services import audit_service, bundle_service


def list_bundles(db: Session) -> dict[str, Any]:
    return {"items": bundle_service.list_bundles(db)}


def preview(db: Session, payload: BundlePreviewRequest) -> dict[str, Any]:
    return bundle_service.preview(db, payload)


def create(db: Session, payload: BundleCreateRequest, user: User, request: Request) -> dict[str, Any]:
    return {"bundle": bundle_service.submit(db, user, payload, request)}


def detail(db: Session, bundle_id: str) -> dict[str, Any]:
    bundle = bundle_service.get_bundle(db, bundle_id)
    requester = db.get(User, bundle.requester_id)
    reviewer = db.get(User, bundle.reviewed_by) if bundle.reviewed_by else None
    return {"bundle": bundle_service.serialize(bundle, requester, reviewer)}


def approve(
    db: Session, bundle_id: str, payload: ReviewRequest, user: User, request: Request
) -> dict[str, Any]:
    return {"bundle": bundle_service.approve(db, user, bundle_id, payload.note, request)}


def reject(
    db: Session, bundle_id: str, payload: ReviewRequest, user: User, request: Request
) -> dict[str, Any]:
    return {"bundle": bundle_service.reject(db, user, bundle_id, payload.note, request)}


def retry(db: Session, bundle_id: str, user: User, request: Request) -> dict[str, Any]:
    return {"bundle": bundle_service.retry(db, user, bundle_id, request)}


def download(
    db: Session, bundle_id: str, user: User, request: Request, table_name: str | None = None
) -> FileResponse:
    path, bundle = bundle_service.artifact_path(db, bundle_id, user, table_name)
    audit_service.record(
        db,
        action="bundle.download",
        actor=user,
        entity_type="cohort_bundle",
        entity_id=str(bundle.id),
        detail={
            "scope_key": bundle.scope_key,
            "table": table_name or "bundle.zip",
            "digest": bundle.file_digest,
            "rows": bundle.retained_rows,
        },
        request=request,
    )
    db.commit()

    suffix = bundle.scope_key[:12]
    if table_name is None:
        return FileResponse(
            path=Path(path),
            media_type="application/zip",
            filename=f"maskguard-joint-bundle-{suffix}.zip",
        )
    return FileResponse(
        path=Path(path),
        media_type="text/csv",
        filename=f"maskguard-{table_name}-{suffix}.csv",
    )
