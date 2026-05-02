# ============================================================
# File   : trading/summary/persistence/core/sql_builder.py
# Version: PRODUCTION-STABLE-REV1.0-SUMMARY-SQL-BUILDER
# ============================================================

from __future__ import annotations

from typing import Sequence

from database.sqlite.sql_builder import (
    build_delete_by_columns_sql,
    build_insert_sql,
    build_sqlite_upsert_sql,
)


def build_summary_upsert_sql(table_name: str, rows: Sequence[dict]) -> str:
    return build_sqlite_upsert_sql(
        table_name,
        rows,
        conflict_cols=("symbol", "datetime"),
        exclude_update_cols=("symbol", "datetime"),
    )


def build_summary_insert_sql(table_name: str, rows: Sequence[dict]) -> str:
    return build_insert_sql(table_name, rows)


def build_summary_delete_sql(table_name: str) -> str:
    return build_delete_by_columns_sql(table_name, ("symbol", "datetime"))
