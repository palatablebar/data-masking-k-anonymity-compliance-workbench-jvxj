from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.controllers import bundle_controller
from src.middlewares.rate_limit import rate_limit
from src.middlewares.rbac import require_permission
from src.models.user import User
from src.schemas.bundle import BundleCreateRequest, BundlePreviewRequest
from src.schemas.export import ReviewRequest

router = APIRouter(prefix="/bundles", tags=["bundles"])


@router.get("")
def list_bundles(
    user: User = Depends(require_permission("exports:read")), db: Session = Depends(get_db)
) -> dict:
    return bundle_controller.list_bundles(db)


@router.post("/preview")
def preview(
    payload: BundlePreviewRequest,
    user: User = Depends(require_permission("exports:create")),
    db: Session = Depends(get_db),
    _: None = rate_limit("bundle"),
) -> dict:
    return bundle_controller.preview(db, payload)


@router.post("")
def create(
    payload: BundleCreateRequest,
    request: Request,
    user: User = Depends(require_permission("exports:create")),
    db: Session = Depends(get_db),
    _: None = rate_limit("bundle"),
) -> dict:
    return bundle_controller.create(db, payload, user, request)


@router.get("/{bundle_id}")
def detail(
    bundle_id: str,
    user: User = Depends(require_permission("exports:read")),
    db: Session = Depends(get_db),
) -> dict:
    return bundle_controller.detail(db, bundle_id)


@router.post("/{bundle_id}/approve")
def approve(
    bundle_id: str,
    payload: ReviewRequest,
    request: Request,
    user: User = Depends(require_permission("exports:approve")),
    db: Session = Depends(get_db),
) -> dict:
    return bundle_controller.approve(db, bundle_id, payload, user, request)


@router.post("/{bundle_id}/reject")
def reject(
    bundle_id: str,
    payload: ReviewRequest,
    request: Request,
    user: User = Depends(require_permission("exports:approve")),
    db: Session = Depends(get_db),
) -> dict:
    return bundle_controller.reject(db, bundle_id, payload, user, request)


@router.post("/{bundle_id}/retry")
def retry(
    bundle_id: str,
    request: Request,
    user: User = Depends(require_permission("exports:create")),
    db: Session = Depends(get_db),
    _: None = rate_limit("bundle"),
) -> dict:
    return bundle_controller.retry(db, bundle_id, user, request)


@router.get("/{bundle_id}/download")
def download(
    bundle_id: str,
    request: Request,
    user: User = Depends(require_permission("exports:read")),
    db: Session = Depends(get_db),
) -> FileResponse:
    return bundle_controller.download(db, bundle_id, user, request)


@router.get("/{bundle_id}/download/{table_name}")
def download_table(
    bundle_id: str,
    table_name: str,
    request: Request,
    user: User = Depends(require_permission("exports:read")),
    db: Session = Depends(get_db),
) -> FileResponse:
    return bundle_controller.download(db, bundle_id, user, request, table_name)
