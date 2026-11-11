"""上游业务库（clinic schema）的只读访问。

原始行不复制进元数据库：算法与导出都直接读 clinic 表，用参数化查询 + 标识符白名单。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.utils.sql_safety import qualified_table, safe_identifier


def column_values(db: Session, table_name: str, column_name: str, limit: int) -> list[str]:
    sql = text(
        f"SELECT {safe_identifier(column_name)} FROM {qualified_table(settings.raw_schema, table_name)} "
        f"LIMIT :limit"
    )
    rows = db.execute(sql, {"limit": limit}).scalars().all()
    return ["" if value is None else str(value) for value in rows]


def row_count(db: Session, table_name: str) -> int:
    sql = text(f"SELECT count(*) FROM {qualified_table(settings.raw_schema, table_name)}")
    return int(db.execute(sql).scalar_one())


def fetch_rows(
    db: Session,
    table_name: str,
    columns: list[str],
    limit: int,
    order_by: str | None = None,
) -> list[dict[str, Any]]:
    select_list = ", ".join(safe_identifier(column) for column in columns)
    order_clause = f" ORDER BY {safe_identifier(order_by)}" if order_by else ""
    sql = text(
        f"SELECT {select_list} FROM {qualified_table(settings.raw_schema, table_name)}"
        f"{order_clause} LIMIT :limit"
    )
    result = db.execute(sql, {"limit": limit})
    return [dict(row._mapping) for row in result]


def column_stats(db: Session, table_name: str, column_name: str, limit: int) -> dict[str, Any]:
    """空值率 / 唯一性 / 样例：扫描阶段做列画像用。"""
    values = column_values(db, table_name, column_name, limit)
    total = len(values)
    non_empty = [value for value in values if value.strip() != ""]
    distinct = len(set(non_empty))
    return {
        "total": total,
        "null_ratio": round((total - len(non_empty)) / total, 4) if total else 0.0,
        "distinct_ratio": round(distinct / total, 4) if total else 0.0,
        "distinct": distinct,
        "samples": non_empty[:8],
    }


# ---------------------------------------------------------------- 联合去标识包 ----
# 三张表都以 patient_id 作为范围关联键；下面两个函数只接受已解析的患者编号集合，
# 全部走参数化查询，不把外部输入拼进 SQL。


def patient_ids_in_range(
    db: Session, start: str | None = None, end: str | None = None, match_all: bool = False
) -> list[str]:
    """按编号区间/全量读取患者主索引里真实存在的 patient_id（用于范围解析）。"""
    table = qualified_table(settings.raw_schema, "patients")
    where: list[str] = []
    params: dict[str, Any] = {}
    if not match_all:
        if start:
            where.append("patient_id >= :start")
            params["start"] = start
        if end:
            where.append("patient_id <= :end")
            params["end"] = end
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    sql = text(f"SELECT patient_id FROM {table}{clause} ORDER BY patient_id")
    return [str(value) for value in db.execute(sql, params).scalars().all()]


def fetch_rows_for_patients(
    db: Session,
    table_name: str,
    columns: list[str],
    patient_ids: list[str],
    order_by: str,
    limit: int,
) -> list[dict[str, Any]]:
    """读取 patient_id 落在范围内的行；多取 1 行用于在 service 层判断是否超限。"""
    if not patient_ids:
        return []
    select_list = ", ".join(safe_identifier(column) for column in columns)
    table = qualified_table(settings.raw_schema, table_name)
    sql = text(
        f"SELECT {select_list} FROM {table} "
        "WHERE patient_id IN :patient_ids "
        f"ORDER BY {safe_identifier(order_by)} LIMIT :limit"
    ).bindparams(bindparam("patient_ids", expanding=True))
    result = db.execute(sql, {"patient_ids": patient_ids, "limit": int(limit)})
    return [dict(row._mapping) for row in result]
