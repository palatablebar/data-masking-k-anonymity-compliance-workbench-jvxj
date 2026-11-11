from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin, new_uuid


class CohortBundle(Base, TimestampMixin):
    """联合去标识包：同一患者范围下 patients / visits / labs 三表共用稳定假名。

    scope_key 是对“范围 + 选项 + 数据集版本”的规范哈希。同范围重复发起时复用同一条记录，
    因此包编号、假名与文件路径都不会漂移；失败重试仍在同一条记录、同一个最终目录上完成。
    """

    __tablename__ = "cohort_bundles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    scope_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    requester_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(400), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(40), nullable=False, default="2026-11")
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    build_status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle", index=True)
    review_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 指向 bundle.zip；同目录下另有 patients.csv / visits.csv / labs.csv / manifest.json
    file_path: Mapped[str | None] = mapped_column(String(400), nullable=True)
    file_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retained_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
