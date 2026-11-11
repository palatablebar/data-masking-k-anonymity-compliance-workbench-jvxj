"""联合去标识包：按患者范围联动三张表，稳定假名 + 原子发布。

关键口径：
1. 范围 = 一组 patient_id（显式列表 / 编号区间 / 全量，可并集），按规范 JSON 哈希成 scope_key。
2. 同一 scope_key 永远复用同一条记录；假名由 HMAC-SHA256(盐, "pseudonym-v1:patient_id")
   确定性派生，patients / visits / labs 三表共用，补发、重跑都不漂移。
3. visits / labs 只保留范围内且关联完整的行；缺失原因逐表计数。
4. 所有文件先写 staging 目录，全部成功后才整目录原子换入最终目录；失败清理 staging，
   DB 标记 failed，可原地重试，不会留下半套包。
5. 文件内容不写入时间戳、不依赖请求 ID，行序按主键固定，同范围重复生成字节一致。
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import zipfile
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config.celery_app import dispatch
from src.config.settings import settings
from src.constants import error_codes as codes
from src.constants.enums import ColumnRole, ExportStatus
from src.constants.limits import BUNDLE_MAX_PATIENTS, BUNDLE_MAX_ROWS
from src.models.cohort_bundle import CohortBundle
from src.models.dataset_column import DatasetColumn
from src.models.dataset_table import DatasetTable
from src.models.user import User
from src.models.base import utcnow
from src.schemas.bundle import BundleCreateRequest, BundlePreviewRequest, BundleScopeRequest
from src.schemas.common import iso
from src.services import audit_service, masking_service, policy_service, raw_data_service
from src.utils.checksum import sha256_bytes, sha256_text
from src.utils.errors import AppError, not_found
from src.utils.logger import get_logger, log_error, log_info

logger = get_logger("maskguard.bundle")

BUNDLE_TASK = "maskguard.build_bundle"
PSEUDONYM_VERSION = "pseudonym-v1"
PSEUDONYM_PREFIX = "SUBJ-"
TABLE_ORDER = ("patients", "visits", "labs")
TABLE_KEYS = {"patients": "patient_id", "visits": "visit_id", "labs": "lab_id"}
DATE_GRANULARITIES = {"year", "month"}
_DIRECT_ID_PLACEHOLDER = "[已抑制]"
_ID_SPLIT = re.compile(r"[,\s;，；]+")


# ------------------------------------------------------------------ 路径 / 锁 ----


def bundles_dir() -> Path:
    path = Path(settings.artifact_dir) / "bundles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def final_dir(bundle_id: Any) -> Path:
    return bundles_dir() / str(bundle_id)


def _staging_dir(bundle_id: Any, attempt: int) -> Path:
    staging_root = bundles_dir() / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    # pid 后缀避免极端情况下多个进程撞名；staging 永远不会被下载接口暴露
    return staging_root / f"{bundle_id}-{attempt}-{os.getpid()}"


def _advisory_key(bundle_id: Any) -> int:
    return int(hashlib.sha256(f"bundle:{bundle_id}".encode("utf-8")).hexdigest()[:15], 16)


def _try_advisory_lock(db: Session, key: int) -> bool | None:
    """Postgres 会话级 advisory lock；返回 None 表示当前数据库不支持（测试/降级时放行）。"""
    try:
        return bool(db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar())
    except Exception:  # noqa: BLE001 - 非 Postgres 环境不阻塞业务
        return None


def _release_advisory_lock(db: Session, key: int) -> None:
    try:
        db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


# --------------------------------------------------------------- 范围解析 ----


def _normalize_ids(values: list[str] | str | None) -> list[str]:
    found: set[str] = set()
    if isinstance(values, str):
        values = [values]
    for raw in values or []:
        if raw is None:
            continue
        for token in _ID_SPLIT.split(str(raw)):
            token = token.strip()
            if token:
                found.add(token)
    return sorted(found)


def resolve_scope(
    db: Session, spec: BundleScopeRequest, dataset_version: str
) -> tuple[str, dict[str, Any]]:
    """把范围规格解析成规范患者集合与幂等 scope_key。"""
    granularity = str(spec.date_granularity or "year").strip().lower()
    if granularity not in DATE_GRANULARITIES:
        raise AppError(codes.VALIDATION_ERROR, "日期泛化粒度只支持 year / month", granularity)

    explicit = _normalize_ids(spec.patient_ids)
    if len(explicit) > BUNDLE_MAX_PATIENTS:
        raise AppError(
            codes.BUNDLE_TOO_LARGE,
            None,
            f"explicit patients={len(explicit)} > {BUNDLE_MAX_PATIENTS}",
        )

    start = (spec.patient_id_start or "").strip() or None
    end = (spec.patient_id_end or "").strip() or None
    if start and end and start > end:
        raise AppError(codes.BUNDLE_INVALID_SCOPE, "患者编号区间起点不能大于终点", f"{start} > {end}")

    resolved_from_source: list[str] = []
    if spec.all_patients or start or end:
        resolved_from_source = raw_data_service.patient_ids_in_range(
            db, start=start, end=end, match_all=spec.all_patients
        )

    scope_ids = sorted(set(explicit) | set(resolved_from_source))
    if not scope_ids:
        raise AppError(
            codes.BUNDLE_INVALID_SCOPE,
            None,
            "no patient_ids / range / all_patients criteria resolved to any patient",
        )
    if len(scope_ids) > BUNDLE_MAX_PATIENTS:
        raise AppError(
            codes.BUNDLE_TOO_LARGE,
            None,
            f"scope patients={len(scope_ids)} > {BUNDLE_MAX_PATIENTS}",
        )

    if spec.all_patients:
        mode = "all"
    elif (start or end) and explicit:
        mode = "mixed"
    elif start or end:
        mode = "range"
    else:
        mode = "ids"

    canonical = json.dumps(
        {
            "dataset_version": dataset_version,
            "date_granularity": granularity,
            "patient_ids": scope_ids,
            "pseudonym_version": PSEUDONYM_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    scope_key = sha256_text(canonical)
    scope_json: dict[str, Any] = {
        "scope_key": scope_key,
        "mode": mode,
        "patient_count": len(scope_ids),
        "date_granularity": granularity,
        "dataset_version": dataset_version,
        "pseudonym_version": PSEUDONYM_VERSION,
        "patient_ids": scope_ids,
        "requested": {
            "patient_ids": explicit,
            "patient_id_start": start,
            "patient_id_end": end,
            "all_patients": bool(spec.all_patients),
        },
    }
    return scope_key, scope_json


# --------------------------------------------------------------- 读取 / 统计 ----


def _dataset_columns(db: Session, table_name: str) -> tuple[DatasetTable, list[DatasetColumn]]:
    table = db.query(DatasetTable).filter(DatasetTable.table_name == table_name).one_or_none()
    if table is None:
        raise not_found("数据集表", table_name)
    columns = (
        db.query(DatasetColumn)
        .filter(DatasetColumn.table_id == table.id)
        .order_by(DatasetColumn.ordinal)
        .all()
    )
    if not columns:
        raise AppError(codes.VALIDATION_ERROR, f"数据集表 {table_name} 尚无列画像", "empty columns")
    return table, columns


def _stat(
    table_name: str,
    *,
    source_rows: int,
    input_rows: int,
    retained_rows: int,
    missing_reasons: Counter[str],
) -> dict[str, Any]:
    return {
        "table_name": table_name,
        "source_rows": int(source_rows),
        "input_rows": int(input_rows),
        "retained_rows": int(retained_rows),
        "missing_rows": int(input_rows - retained_rows),
        "missing_reasons": dict(sorted(missing_reasons.items())),
    }


def _collect_plan(db: Session, scope_ids: list[str], full_rows: bool) -> dict[str, Any]:
    """读取范围内候选行 → 校验关联完整性 → 产出各表输入/保留/缺失统计与待写行。"""
    columns_by_table = {name: _dataset_columns(db, name)[1] for name in TABLE_ORDER}
    if full_rows:
        select_names = {name: [column.name for column in columns_by_table[name]] for name in TABLE_ORDER}
    else:
        select_names = {
            "patients": ["patient_id"],
            "visits": ["visit_id", "patient_id"],
            "labs": ["lab_id", "visit_id", "patient_id"],
        }

    candidates: dict[str, list[dict[str, Any]]] = {}
    source_counts: dict[str, int] = {}
    for name in TABLE_ORDER:
        rows = raw_data_service.fetch_rows_for_patients(
            db,
            name,
            select_names[name],
            scope_ids,
            TABLE_KEYS[name],
            BUNDLE_MAX_ROWS + 1,
        )
        if len(rows) > BUNDLE_MAX_ROWS:
            raise AppError(
                codes.BUNDLE_TOO_LARGE,
                None,
                f"table={name} candidate rows > {BUNDLE_MAX_ROWS}",
            )
        candidates[name] = rows
        source_counts[name] = raw_data_service.row_count(db, name)

    found = {str(row.get("patient_id")): row for row in candidates["patients"]}
    missing_patients = [patient_id for patient_id in scope_ids if patient_id not in found]

    visit_rows = candidates["visits"]
    visit_reasons: Counter[str] = Counter()
    included_visits: list[dict[str, Any]] = []
    for row in visit_rows:
        patient_id = str(row.get("patient_id"))
        if patient_id not in found:
            visit_reasons["patient_not_found"] += 1
            continue
        included_visits.append(row)

    visit_patient = {str(row.get("visit_id")): str(row.get("patient_id")) for row in included_visits}

    lab_rows = candidates["labs"]
    lab_reasons: Counter[str] = Counter()
    included_labs: list[dict[str, Any]] = []
    for row in lab_rows:
        patient_id = str(row.get("patient_id") or "")
        visit_id = str(row.get("visit_id"))
        if patient_id not in found:
            lab_reasons["patient_not_found"] += 1
            continue
        if visit_id not in visit_patient:
            lab_reasons["visit_not_in_scope"] += 1
            continue
        if visit_patient[visit_id] != patient_id:
            lab_reasons["patient_visit_mismatch"] += 1
            continue
        included_labs.append(row)

    visit_patient_ids = {str(row.get("patient_id")) for row in included_visits}
    lab_patient_ids = {str(row.get("patient_id")) for row in included_labs}

    stats = {
        "patients": _stat(
            "patients",
            source_rows=source_counts["patients"],
            input_rows=len(scope_ids),
            retained_rows=len(found),
            missing_reasons=Counter({"patient_not_found": len(missing_patients)})
            if missing_patients
            else Counter(),
        ),
        "visits": _stat(
            "visits",
            source_rows=source_counts["visits"],
            input_rows=len(visit_rows),
            retained_rows=len(included_visits),
            missing_reasons=visit_reasons,
        ),
        "labs": _stat(
            "labs",
            source_rows=source_counts["labs"],
            input_rows=len(lab_rows),
            retained_rows=len(included_labs),
            missing_reasons=lab_reasons,
        ),
        "gaps": {
            "missing_patient_count": len(missing_patients),
            "missing_patient_ids": missing_patients[:200],
            "missing_patient_ids_truncated": len(missing_patients) > 200,
            "patients_without_visits": sum(1 for patient_id in found if patient_id not in visit_patient_ids),
            "patients_without_labs": sum(1 for patient_id in found if patient_id not in lab_patient_ids),
        },
    }
    stats["totals"] = {
        key: sum(int(stats[name][key]) for name in TABLE_ORDER)
        for key in ("source_rows", "input_rows", "retained_rows", "missing_rows")
    }

    return {
        "rows": {
            "patients": candidates["patients"],
            "visits": included_visits,
            "labs": included_labs,
        },
        "columns": columns_by_table,
        "stats": stats,
    }


# --------------------------------------------------------------- 值变换 / 写包 ----


def pseudonymize(patient_id: Any) -> str:
    """稳定假名：同一 salt 下同一 patient_id 永远得到同一编号；三表共用。"""
    raw = "" if patient_id is None else str(patient_id)
    digest = hmac.new(
        settings.hash_salt.encode("utf-8"),
        f"{PSEUDONYM_VERSION}:{raw}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{PSEUDONYM_PREFIX}{digest[:16].upper()}"


def _canonical_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text_value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text_value, fmt).date()
        except ValueError:
            continue
    return None


def _generalize_date(value: Any, granularity: str) -> str:
    parsed = _as_date(value)
    if parsed is None:
        return _canonical_value(value)
    if granularity == "month":
        return f"{parsed.year:04d}-{parsed.month:02d}"
    return f"{parsed.year:04d}"


def _is_related_date_column(table_name: str, column: DatasetColumn) -> bool:
    if table_name not in ("visits", "labs"):
        return False
    return column.data_type.lower() == "date" or column.name.endswith("_date")


def _render_table(
    db: Session,
    table_name: str,
    columns: list[DatasetColumn],
    rows: list[dict[str, Any]],
    granularity: str,
) -> bytes:
    policies = {column.id: policy_service.policy_for_column(db, column) for column in columns}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([column.name for column in columns])
    for row in rows:
        cells: list[str] = []
        for column in columns:
            raw = row.get(column.name)
            canonical = _canonical_value(raw)
            if column.name == "patient_id":
                cell = pseudonymize(raw)
            elif _is_related_date_column(table_name, column):
                # 关联表的日期一律降精度，避免“患者编号 + 日期”重新拼回身份
                cell = _generalize_date(raw, granularity)
            elif (policy := policies.get(column.id)) is not None:
                cell = masking_service.mask_value_with_policy(column.name, canonical, policy)
            elif column.column_role == ColumnRole.DIRECT_ID.value:
                # 没有配置策略的直接标识列也不能裸奔
                cell = _DIRECT_ID_PLACEHOLDER
            else:
                cell = canonical
            cells.append(cell)
        writer.writerow(cells)
    return buffer.getvalue().encode("utf-8")


def _build_manifest(
    scope_json: dict[str, Any],
    stats: dict[str, Any],
    columns_by_table: dict[str, list[DatasetColumn]],
    table_digests: dict[str, str],
) -> tuple[bytes, str]:
    tables_out: dict[str, Any] = {}
    for name in TABLE_ORDER:
        table_stat = dict(stats[name])
        table_stat["file"] = f"{name}.csv"
        table_stat["sha256"] = table_digests[name]
        table_stat["columns"] = [column.name for column in columns_by_table[name]]
        tables_out[name] = table_stat

    manifest = {
        "format": "maskguard.joint_bundle.v1",
        "scope_key": scope_json.get("scope_key", ""),
        "dataset_version": scope_json.get("dataset_version", ""),
        "date_granularity": scope_json.get("date_granularity", "year"),
        "pseudonym": {
            "algorithm": "HMAC-SHA256",
            "version": PSEUDONYM_VERSION,
            "prefix": PSEUDONYM_PREFIX,
            "stable_across_runs": True,
            "shared_tables": list(TABLE_ORDER),
        },
        "tables": tables_out,
        "totals": stats["totals"],
        "gaps": stats["gaps"],
    }
    payload = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": ")) + "\n"
    ).encode("utf-8")
    return payload, sha256_bytes(payload)


def _write_zip(staging: Path) -> bytes:
    """固定条目顺序 / 时间戳 / 属性，保证同一包重复生成 zip 字节一致。"""
    archive = staging / "bundle.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zip_file:
        for name in [f"{table}.csv" for table in TABLE_ORDER] + ["manifest.json"]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            zip_file.writestr(info, (staging / name).read_bytes())
    return archive.read_bytes()


def _publish(staging: Path, target: Path) -> None:
    """整目录发布：staging 里所有文件都成功后才出现在最终目录。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    os.replace(staging, target)


# --------------------------------------------------------------- 业务入口 ----


def get_bundle(db: Session, bundle_id: str) -> CohortBundle:
    bundle = db.get(CohortBundle, bundle_id)
    if bundle is None:
        raise not_found("联合去标识包", bundle_id)
    return bundle


def _scope_out(scope_json: dict[str, Any], scope_key: str | None = None) -> dict[str, Any]:
    patient_ids = [str(item) for item in (scope_json.get("patient_ids") or [])]
    return {
        "scope_key": scope_key or scope_json.get("scope_key", ""),
        "mode": scope_json.get("mode", "ids"),
        "patient_count": int(scope_json.get("patient_count", len(patient_ids))),
        "date_granularity": scope_json.get("date_granularity", "year"),
        "dataset_version": scope_json.get("dataset_version", ""),
        "pseudonym_version": scope_json.get("pseudonym_version", PSEUDONYM_VERSION),
        "patient_ids_preview": patient_ids[:20],
        "requested": dict(scope_json.get("requested") or {}),
    }


def serialize(bundle: CohortBundle, requester: Any, reviewer: Any) -> dict[str, Any]:
    stats = dict(bundle.stats_json or {})
    return {
        "id": str(bundle.id),
        "scope_key": bundle.scope_key,
        "scope": _scope_out(dict(bundle.scope_json or {}), bundle.scope_key),
        "purpose": bundle.purpose,
        "dataset_version": bundle.dataset_version,
        "requester_id": str(bundle.requester_id),
        "requester_email": requester.email if requester else "",
        "requester_name": requester.display_name if requester else "",
        "status": bundle.status,
        "build_status": bundle.build_status,
        "review_note": bundle.review_note,
        "reviewed_by_email": reviewer.email if reviewer else None,
        "reviewed_at": iso(bundle.reviewed_at),
        "file_digest": bundle.file_digest,
        "manifest_digest": bundle.manifest_digest,
        "retained_rows": bundle.retained_rows,
        "attempt_count": bundle.attempt_count,
        "error_detail": bundle.error_detail,
        "stats": stats,
        "download_ready": bundle.status == ExportStatus.APPROVED.value
        and bundle.build_status == "done"
        and bool(bundle.file_path),
        "can_retry": bundle.status == ExportStatus.APPROVED.value
        and bundle.build_status in ("failed", "idle", "queued"),
        "created_at": iso(bundle.created_at),
        "updated_at": iso(bundle.updated_at),
    }


def list_bundles(db: Session) -> list[dict[str, Any]]:
    rows = (
        db.query(CohortBundle, User)
        .join(User, User.id == CohortBundle.requester_id)
        .order_by(CohortBundle.created_at.desc())
        .all()
    )
    reviewer_ids = {bundle.reviewed_by for bundle, _ in rows if bundle.reviewed_by}
    reviewers = {
        user.id: user for user in db.query(User).filter(User.id.in_(reviewer_ids)).all()
    } if reviewer_ids else {}
    return [
        serialize(bundle, requester, reviewers.get(bundle.reviewed_by))
        for bundle, requester in rows
    ]


def _find_existing(db: Session, scope_key: str) -> CohortBundle | None:
    return db.query(CohortBundle).filter(CohortBundle.scope_key == scope_key).one_or_none()


def preview(db: Session, payload: BundlePreviewRequest) -> dict[str, Any]:
    """只算缺口、不落库；页面可在发起前看各表输入 / 保留 / 缺失。"""
    dataset_version = (payload.dataset_version or "2026-11").strip() or "2026-11"
    scope_key, scope_json = resolve_scope(db, payload.resolved_scope(), dataset_version)
    plan = _collect_plan(db, list(scope_json["patient_ids"]), full_rows=False)
    existing = _find_existing(db, scope_key)
    return {
        "scope": _scope_out(scope_json, scope_key),
        "stats": plan["stats"],
        "existing_bundle_id": str(existing.id) if existing else None,
        "existing_status": existing.status if existing else None,
    }


def submit(db: Session, user: User, payload: BundleCreateRequest, request=None) -> dict[str, Any]:
    dataset_version = (payload.dataset_version or "2026-11").strip() or "2026-11"
    scope_key, scope_json = resolve_scope(db, payload.resolved_scope(), dataset_version)

    existing = _find_existing(db, scope_key)
    if existing is not None:
        requester = db.get(User, existing.requester_id)
        reviewer = db.get(User, existing.reviewed_by) if existing.reviewed_by else None
        return serialize(existing, requester, reviewer)

    plan = _collect_plan(db, list(scope_json["patient_ids"]), full_rows=False)
    bundle = CohortBundle(
        scope_key=scope_key,
        requester_id=user.id,
        purpose=payload.purpose,
        dataset_version=dataset_version,
        scope_json=scope_json,
        stats_json=plan["stats"],
        status=ExportStatus.PENDING.value,
        build_status="idle",
    )
    db.add(bundle)
    db.flush()
    audit_service.record(
        db,
        action="bundle.submit",
        actor=user,
        entity_type="cohort_bundle",
        entity_id=str(bundle.id),
        detail={
            "scope_key": scope_key,
            "patients": scope_json["patient_count"],
            "date_granularity": scope_json["date_granularity"],
            "input_rows": plan["stats"]["totals"]["input_rows"],
        },
        request=request,
    )
    try:
        db.commit()
    except IntegrityError:
        # 并发同范围提交：唯一约束兜底，回读已有记录
        db.rollback()
        existing = _find_existing(db, scope_key)
        if existing is None:
            raise
        requester = db.get(User, existing.requester_id)
        reviewer = db.get(User, existing.reviewed_by) if existing.reviewed_by else None
        return serialize(existing, requester, reviewer)
    db.refresh(bundle)

    # 具备审批权限的角色发起时直接放行，避免同一人来回点两次；client 仍走审批。
    if user.role in ("admin", "dpo"):
        return approve(db, user, str(bundle.id), "发起人具备审批权限，自动批准", request)

    requester = db.get(User, user.id)
    return serialize(bundle, requester, None)


def _dispatch_build(db: Session, bundle: CohortBundle) -> None:
    task_id = dispatch(BUNDLE_TASK, bundle_id=str(bundle.id))
    if task_id is None:
        # broker 不可用：同步兜底；失败已落库，可由 retry 原地重试
        build_bundle(db, str(bundle.id))
        return
    bundle.build_status = "queued"
    db.commit()


def approve(
    db: Session, reviewer: User, bundle_id: str, note: str, request=None
) -> dict[str, Any]:
    bundle = get_bundle(db, bundle_id)
    if bundle.status != ExportStatus.PENDING.value:
        raise AppError(codes.BUNDLE_NOT_PENDING, None, f"status={bundle.status}")
    bundle.status = ExportStatus.APPROVED.value
    bundle.build_status = "queued"
    bundle.review_note = note
    bundle.reviewed_by = reviewer.id
    bundle.reviewed_at = utcnow()
    audit_service.record(
        db,
        action="bundle.approve",
        actor=reviewer,
        entity_type="cohort_bundle",
        entity_id=str(bundle.id),
        detail={"scope_key": bundle.scope_key, "note": note},
        request=request,
    )
    db.commit()

    _dispatch_build(db, bundle)
    requester = db.get(User, bundle.requester_id)
    db.refresh(bundle)
    return serialize(bundle, requester, reviewer)


def reject(db: Session, reviewer: User, bundle_id: str, note: str, request=None) -> dict[str, Any]:
    bundle = get_bundle(db, bundle_id)
    if bundle.status != ExportStatus.PENDING.value:
        raise AppError(codes.BUNDLE_NOT_PENDING, None, f"status={bundle.status}")
    bundle.status = ExportStatus.REJECTED.value
    bundle.review_note = note
    bundle.reviewed_by = reviewer.id
    bundle.reviewed_at = utcnow()
    audit_service.record(
        db,
        action="bundle.reject",
        actor=reviewer,
        entity_type="cohort_bundle",
        entity_id=str(bundle.id),
        detail={"scope_key": bundle.scope_key, "note": note},
        request=request,
    )
    db.commit()
    requester = db.get(User, bundle.requester_id)
    db.refresh(bundle)
    return serialize(bundle, requester, reviewer)


def retry(db: Session, user: User, bundle_id: str, request=None) -> dict[str, Any]:
    bundle = get_bundle(db, bundle_id)
    if not (user.role in ("admin", "dpo") or str(bundle.requester_id) == str(user.id)):
        raise AppError(codes.BUNDLE_DOWNLOAD_DENIED, "无权重试该联合包", f"user={user.email}")
    if bundle.status != ExportStatus.APPROVED.value:
        raise AppError(codes.BUNDLE_NOT_PENDING, None, f"status={bundle.status}")
    if bundle.build_status == "done":
        requester = db.get(User, bundle.requester_id)
        reviewer = db.get(User, bundle.reviewed_by) if bundle.reviewed_by else None
        return serialize(bundle, requester, reviewer)
    if bundle.build_status == "running":
        raise AppError(codes.BUNDLE_ALREADY_BUILDING, None, "build already running")

    bundle.build_status = "queued"
    bundle.error_detail = None
    audit_service.record(
        db,
        action="bundle.retry",
        actor=user,
        entity_type="cohort_bundle",
        entity_id=str(bundle.id),
        detail={"scope_key": bundle.scope_key, "attempt": bundle.attempt_count},
        request=request,
    )
    db.commit()

    _dispatch_build(db, bundle)
    requester = db.get(User, bundle.requester_id)
    reviewer = db.get(User, bundle.reviewed_by) if bundle.reviewed_by else None
    db.refresh(bundle)
    return serialize(bundle, requester, reviewer)


def build_bundle(db: Session, bundle_id: str) -> dict[str, Any]:
    """生成联合包。失败只落 failed 状态并清理 staging，不抛异常（便于 API/worker 统一重试）。"""
    bundle = get_bundle(db, bundle_id)
    if bundle.build_status == "done":
        return {
            "status": "already_done",
            "bundle_id": str(bundle.id),
            "digest": bundle.file_digest,
        }
    if bundle.status != ExportStatus.APPROVED.value:
        return {"status": "not_approved", "bundle_id": str(bundle.id), "status_detail": bundle.status}
    if bundle.build_status == "running":
        return {"status": "busy", "bundle_id": str(bundle.id)}

    lock_key = _advisory_key(bundle.id)
    locked = _try_advisory_lock(db, lock_key)
    if locked is False:
        return {"status": "busy", "bundle_id": str(bundle.id)}

    staging: Path | None = None
    try:
        bundle.build_status = "running"
        bundle.attempt_count = int(bundle.attempt_count or 0) + 1
        bundle.error_detail = None
        db.commit()

        scope_json = dict(bundle.scope_json or {})
        scope_ids = [str(item) for item in (scope_json.get("patient_ids") or [])]
        granularity = str(scope_json.get("date_granularity") or "year")
        plan = _collect_plan(db, scope_ids, full_rows=True)

        staging = _staging_dir(bundle.id, bundle.attempt_count)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        table_digests: dict[str, str] = {}
        for name in TABLE_ORDER:
            payload = _render_table(
                db, name, plan["columns"][name], plan["rows"][name], granularity
            )
            (staging / f"{name}.csv").write_bytes(payload)
            table_digests[name] = sha256_bytes(payload)

        manifest_payload, manifest_digest = _build_manifest(
            scope_json, plan["stats"], plan["columns"], table_digests
        )
        (staging / "manifest.json").write_bytes(manifest_payload)
        zip_payload = _write_zip(staging)
        zip_digest = sha256_bytes(zip_payload)

        target = final_dir(bundle.id)
        _publish(staging, target)
        staging = None  # 已原子换入最终目录，finally 不再清理

        stats = plan["stats"]
        for name in TABLE_ORDER:
            stats[name]["file"] = f"{name}.csv"
            stats[name]["sha256"] = table_digests[name]
            stats[name]["columns"] = [column.name for column in plan["columns"][name]]
        stats["manifest_sha256"] = manifest_digest
        stats["zip_sha256"] = zip_digest

        bundle.stats_json = stats
        bundle.file_path = str(target / "bundle.zip")
        bundle.file_digest = zip_digest
        bundle.manifest_digest = manifest_digest
        bundle.retained_rows = int(stats["totals"]["retained_rows"])
        bundle.build_status = "done"
        bundle.error_detail = None
        db.commit()

        audit_service.record(
            db,
            action="bundle.build",
            actor=None,
            entity_type="cohort_bundle",
            entity_id=str(bundle.id),
            detail={
                "scope_key": bundle.scope_key,
                "attempt": bundle.attempt_count,
                "retained_rows": bundle.retained_rows,
                "zip_sha256": zip_digest,
            },
        )
        db.commit()
        log_info(
            logger,
            "bundle.build",
            bundle=str(bundle.id),
            patients=scope_json.get("patient_count"),
            rows=bundle.retained_rows,
            digest=zip_digest,
        )
        return {
            "status": "done",
            "bundle_id": str(bundle.id),
            "digest": zip_digest,
            "manifest_digest": manifest_digest,
            "retained_rows": bundle.retained_rows,
        }
    except Exception as exc:  # noqa: BLE001 - 失败必须落库且可重试
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        try:
            bundle = get_bundle(db, bundle_id)
            bundle.build_status = "failed"
            bundle.error_detail = str(exc)[:400]
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        log_error(logger, "bundle.failed", bundle=bundle_id, reason=exc)
        return {"status": "failed", "bundle_id": bundle_id, "error": str(exc)[:400]}
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if locked:
            _release_advisory_lock(db, lock_key)


def artifact_path(
    db: Session, bundle_id: str, user: User, table_name: str | None = None
) -> tuple[Path, CohortBundle]:
    bundle = get_bundle(db, bundle_id)
    if bundle.status != ExportStatus.APPROVED.value:
        raise AppError(codes.BUNDLE_NOT_READY, None, f"status={bundle.status}")
    if bundle.build_status != "done" or not bundle.file_path:
        raise AppError(codes.BUNDLE_NOT_READY, None, f"build_status={bundle.build_status}")
    allowed = user.role in ("admin", "dpo") or str(bundle.requester_id) == str(user.id)
    if not allowed:
        raise AppError(codes.BUNDLE_DOWNLOAD_DENIED, None, f"user={user.email}")

    package_dir = Path(bundle.file_path).parent
    if table_name is None:
        path = Path(bundle.file_path)
    else:
        if table_name not in TABLE_ORDER:
            raise not_found("联合包表文件", table_name)
        path = package_dir / f"{table_name}.csv"
    if not path.exists():
        raise AppError(codes.BUNDLE_NOT_READY, "联合包文件已丢失，请重试生成", str(path))
    return path, bundle
