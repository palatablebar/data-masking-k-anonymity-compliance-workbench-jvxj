"""联合去标识包请求模型。

范围既支持嵌套写法 `{"scope": {...}}`，也支持扁平写法（patient_ids / patient_id_start / ...），
便于前端表单与脚本直接调用。同一条请求里多个条件取并集；完全没有条件则拒绝。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BundleScopeRequest(BaseModel):
    patient_ids: list[str] | str = Field(default_factory=list)
    patient_id_start: str | None = None
    patient_id_end: str | None = None
    all_patients: bool = False
    date_granularity: str = "year"


class BundleCreateRequest(BaseModel):
    purpose: str = Field(default="按患者范围生成联合去标识包", min_length=6, max_length=400)
    dataset_version: str = Field(default="2026-11", max_length=40)
    # 嵌套 scope 优先；否则用下面的扁平字段
    scope: BundleScopeRequest | None = None
    patient_ids: list[str] | str | None = None
    patient_id_start: str | None = None
    patient_id_end: str | None = None
    all_patients: bool = False
    date_granularity: str = "year"

    def resolved_scope(self) -> BundleScopeRequest:
        if self.scope is not None:
            return self.scope
        return BundleScopeRequest(
            patient_ids=self.patient_ids or [],
            patient_id_start=self.patient_id_start,
            patient_id_end=self.patient_id_end,
            all_patients=self.all_patients,
            date_granularity=self.date_granularity,
        )


class BundlePreviewRequest(BaseModel):
    dataset_version: str = Field(default="2026-11", max_length=40)
    scope: BundleScopeRequest | None = None
    patient_ids: list[str] | str | None = None
    patient_id_start: str | None = None
    patient_id_end: str | None = None
    all_patients: bool = False
    date_granularity: str = "year"

    def resolved_scope(self) -> BundleScopeRequest:
        if self.scope is not None:
            return self.scope
        return BundleScopeRequest(
            patient_ids=self.patient_ids or [],
            patient_id_start=self.patient_id_start,
            patient_id_end=self.patient_id_end,
            all_patients=self.all_patients,
            date_granularity=self.date_granularity,
        )
