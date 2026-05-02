# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/sql_helpers.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-SQL-HELPERS
# ------------------------------------------------------------
# 【概要】
#   PUSH DB SQL helper
#
# 【主な機能】
#   ✔ SQLite identifier quote
#   ✔ PRAGMA table_info によるカラム取得
#   ✔ tick_time 候補列による WHERE句生成
#   ✔ symbol 候補列による WHERE句生成
#   ✔ 実在カラムのみで SQL を構築
#
# 【重要】
#   - tick_time 固定SQLは禁止
#   - Symbol / code / symbol_code も実在カラムのみ参照
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from typing import Iterable, Optional

from .constants import (
    PUSH_SYMBOL_COLUMN_CANDIDATES,
    PUSH_TIME_COLUMN_CANDIDATES,
)
from .normalizer import normalize_symbols
from .timezone import format_sql_dt

logger = logging.getLogger(__name__)


def quote_ident(name: str) -> str:
    s = "" if name is None else str(name)
    s = s.replace('"', '""')
    return f'"{s}"'


def fetch_push_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        rows = conn.execute(
            f"PRAGMA table_info({quote_ident(table_name)})"
        ).fetchall()

        return {
            str(r[1]).strip()
            for r in rows
            if len(r) > 1 and r[1] is not None and str(r[1]).strip()
        }

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_push.sql_helpers] failed to inspect table columns: table=%s",
            table_name,
        )
        return set()


def build_push_time_where_clause(
    columns: set[str],
    *,
    start_dt_param: str = "start_dt",
    end_dt_param: str = "end_dt",
    has_start: bool = False,
    has_end: bool = False,
) -> str:
    time_cols = [c for c in PUSH_TIME_COLUMN_CANDIDATES if c in columns]

    if not time_cols:
        return ""

    parts: list[str] = []

    if has_start:
        start_parts = [
            f"{quote_ident(c)} >= :{start_dt_param}"
            for c in time_cols
        ]
        parts.append("(" + " OR ".join(start_parts) + ")")

    if has_end:
        end_parts = [
            f"{quote_ident(c)} <= :{end_dt_param}"
            for c in time_cols
        ]
        parts.append("(" + " OR ".join(end_parts) + ")")

    if not parts:
        return ""

    return " AND ".join(parts)


def build_symbol_where_clause(
    columns: set[str],
    *,
    symbols: Optional[Iterable[str]],
    params: dict,
) -> str:
    symbol_list = normalize_symbols(symbols)
    if not symbol_list:
        return ""

    symbol_cols = [c for c in PUSH_SYMBOL_COLUMN_CANDIDATES if c in columns]
    if not symbol_cols:
        logger.warning(
            "[summary.recovery.loaders_push.sql_helpers] symbol filter requested but no symbol columns exist columns=%s requested_symbols=%d",
            sorted(columns),
            len(symbol_list),
        )
        return ""

    placeholders: list[str] = []
    for idx, sym in enumerate(symbol_list):
        key = f"s{idx}"
        params[key] = sym
        placeholders.append(f":{key}")

    sym_sql = ", ".join(placeholders)

    col_parts = [
        f"{quote_ident(col)} IN ({sym_sql})"
        for col in symbol_cols
    ]

    return "(" + " OR ".join(col_parts) + ")"


def build_db_where_for_push(
    *,
    columns: set[str],
    start_dt=None,
    end_dt=None,
    symbols: Optional[Iterable[str]] = None,
) -> tuple[str, dict]:
    where_parts: list[str] = []
    params: dict = {}

    start_str = format_sql_dt(start_dt, label="db_where.start_dt") if start_dt is not None else None
    end_str = format_sql_dt(end_dt, label="db_where.end_dt") if end_dt is not None else None

    has_start = bool(start_str)
    has_end = bool(end_str)

    if has_start:
        params["start_dt"] = start_str

    if has_end:
        params["end_dt"] = end_str

    time_where = build_push_time_where_clause(
        columns,
        start_dt_param="start_dt",
        end_dt_param="end_dt",
        has_start=has_start,
        has_end=has_end,
    )

    if time_where:
        where_parts.append(time_where)
    elif has_start or has_end:
        logger.warning(
            "[summary.recovery.loaders_push.sql_helpers] time filter requested but no usable time columns exist columns=%s start_dt=%s end_dt=%s",
            sorted(columns),
            start_dt,
            end_dt,
        )

    symbol_where = build_symbol_where_clause(
        columns,
        symbols=symbols,
        params=params,
    )
    if symbol_where:
        where_parts.append(symbol_where)

    where_sql = ""
    if where_parts:
        where_sql = " WHERE " + " AND ".join(where_parts)

    return where_sql, params


# ------------------------------------------------------------
# Backward-compatible aliases
# ------------------------------------------------------------
_quote_ident = quote_ident
_build_symbol_where_clause = build_symbol_where_clause
_build_db_where_for_push = build_db_where_for_push


__all__ = [
    "quote_ident",
    "_quote_ident",
    "fetch_push_table_columns",
    "build_push_time_where_clause",
    "build_symbol_where_clause",
    "build_db_where_for_push",
    "_build_symbol_where_clause",
    "_build_db_where_for_push",
]