"""联合去标识包构建任务：审批/重试后由 worker 生成三表 + manifest + zip。"""
from __future__ import annotations

from worker_src.config.celery_app import celery_app


@celery_app.task(name="maskguard.build_bundle", bind=True)
def build_bundle(self, bundle_id: str) -> dict:
    from src.config.database import session_scope
    from src.models.cohort_bundle import CohortBundle
    from src.services import bundle_service

    with session_scope() as db:
        bundle = db.get(CohortBundle, bundle_id)
        if bundle is None:
            return {"status": "missing", "bundle_id": bundle_id}
        if bundle.build_status == "done":
            return {
                "status": "already_done",
                "bundle_id": bundle_id,
                "digest": bundle.file_digest,
            }
        return bundle_service.build_bundle(db, bundle_id)
