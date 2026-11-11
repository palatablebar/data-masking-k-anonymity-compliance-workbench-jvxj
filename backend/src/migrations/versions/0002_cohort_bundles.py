"""联合去标识包台账 cohort_bundles。

0001 里的 Base.metadata.create_all 会使用当前模型注册表：全新库在 0002 之前可能已经
建出 cohort_bundles。为避免重复建表，这里用 inspector 做存在性判断，保证 upgrade 幂等。

Revision ID: 0002_cohort_bundles
Revises: 0001_initial
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from src.config.settings import settings

revision = "0002_cohort_bundles"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

META = settings.meta_schema


def upgrade() -> None:
    bind = op.get_bind()
    existing = sa.inspect(bind).get_table_names(schema=META)
    if "cohort_bundles" in existing:
        return

    op.create_table(
        "cohort_bundles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "scope_key", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "requester_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{META}.users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=400), nullable=False),
        sa.Column("dataset_version", sa.String(length=40), nullable=False, server_default="2026-11"),
        sa.Column("scope_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stats_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("build_status", sa.String(length=20), nullable=False, server_default="idle"),
        sa.Column("review_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_path", sa.String(length=400), nullable=True),
        sa.Column("file_digest", sa.String(length=64), nullable=True),
        sa.Column("manifest_digest", sa.String(length=64), nullable=True),
        sa.Column("retained_rows", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope_key", name="uq_cohort_bundles_scope_key"),
        schema=META,
    )
    op.create_index(
        "ix_cohort_bundles_requester_id", "cohort_bundles", ["requester_id"], schema=META
    )
    op.create_index("ix_cohort_bundles_status", "cohort_bundles", ["status"], schema=META)
    op.create_index(
        "ix_cohort_bundles_build_status", "cohort_bundles", ["build_status"], schema=META
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {META}.cohort_bundles CASCADE")
