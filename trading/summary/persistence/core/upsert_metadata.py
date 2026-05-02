# ============================================================
# File   : trading/summary/persistence/core/upsert_metadata.py
# Version: Ver1.0-PRODUCTION-UPSERT-METADATA
# ------------------------------------------------------------
# ✔ table/engine/pragma/metadata helper
# ✔ live path では DDL を実行しない
# ✔ unique index は読むだけ
# ============================================================

from __future__ import annotations

import logging
import re

from sqlalchemy import text

logger = logging.getLogger(__name__)

_TABLE_MAP = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
    10: "stock_summary_10min",
    15: "stock_summary_15min",
    30: "stock_summary_30min",
    60: "stock_summary_60min",
    1440: "stock_summary_daily",
}

SQLITE_BUSY_TIMEOUT_MS = 60000


def resolve_table_name(interval: int) -> str:
    interval = int(interval)
    if interval in _TABLE_MAP:
        return _TABLE_MAP[interval]
    return f"stock_summary_{interval}min"


def get_summary_engine():
    try:
        from database.session import summary_engine  # type: ignore
        return summary_engine
    except Exception:
        logger.exception("[UPSERT] summary_engine import failed")
        raise


def apply_sqlite_session_pragmas(conn) -> None:
    pragmas = [
        f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_MS)}",
    ]
    for sql in pragmas:
        try:
            conn.execute(text(sql))
        except Exception:
            logger.debug("[UPSERT] pragma failed sql=%s", sql, exc_info=True)


def fetch_table_columns(conn, table_name: str) -> list[str]:
    try:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        return [str(r[1]) for r in rows if len(r) > 1 and r[1] is not None]
    except Exception:
        logger.exception("[UPSERT] failed fetch table columns table=%s", table_name)
        return []


def fetch_pk_columns(conn, table_name: str) -> list[str]:
    try:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        return [str(r[1]) for r in rows if len(r) > 5 and int(r[5] or 0) > 0]
    except Exception:
        logger.exception("[UPSERT] failed fetch pk columns table=%s", table_name)
        return []


def fetch_unique_indexes(conn, table_name: str) -> list[list[str]]:
    try:
        idx_rows = conn.execute(text(f"PRAGMA index_list({table_name})")).fetchall()
    except Exception:
        logger.exception("[UPSERT] PRAGMA index_list failed table=%s", table_name)
        return []

    unique_sets: list[list[str]] = []

    for row in idx_rows:
        try:
            idx_name = str(row[1])
            is_unique = int(row[2]) == 1
            if not is_unique:
                continue

            info_rows = conn.execute(text(f"PRAGMA index_info({idx_name})")).fetchall()
            cols = [str(r[2]) for r in info_rows if len(r) > 2 and r[2] is not None]
            if cols:
                unique_sets.append(cols)
        except Exception:
            logger.debug(
                "[UPSERT] unique index read failed table=%s row=%s",
                table_name,
                row,
                exc_info=True,
            )

    return unique_sets


def quote_ident(name: str) -> str:
    s = str(name).replace('"', '""')
    return f'"{s}"'


def sanitize_index_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]+", "_", str(name))


def make_unique_index_name(table_name: str, cols: list[str]) -> str:
    base = f"uq_{table_name}_{'_'.join(cols)}"
    return sanitize_index_name(base)[:120]


def same_cols(a: list[str], b: list[str]) -> bool:
    return [str(x).lower() for x in a] == [str(y).lower() for y in b]


def choose_preferred_conflict_target(
    df,
    table_columns: list[str],
    interval: int,
) -> list[str]:
    dfcols = set(df.columns)
    colset = set(table_columns)

    if interval == 1:
        if {"symbol", "datetime"}.issubset(colset) and {"symbol", "datetime"}.issubset(dfcols):
            return ["symbol", "datetime"]

    if {"symbol", "date", "time_range"}.issubset(colset) and {"symbol", "date", "time_range"}.issubset(dfcols):
        return ["symbol", "date", "time_range"]

    if {"symbol", "date", "end_time"}.issubset(colset) and {"symbol", "date", "end_time"}.issubset(dfcols):
        return ["symbol", "date", "end_time"]

    if {"symbol", "date", "time"}.issubset(colset) and {"symbol", "date", "time"}.issubset(dfcols):
        return ["symbol", "date", "time"]

    if {"symbol", "datetime"}.issubset(colset) and {"symbol", "datetime"}.issubset(dfcols):
        return ["symbol", "datetime"]

    raise ValueError(
        f"No preferred conflict target found interval={interval} "
        f"table_columns={table_columns} df_columns={list(df.columns)}"
    )


def choose_conflict_target(
    df,
    table_columns: list[str],
    unique_indexes: list[list[str]],
    interval: int,
) -> list[str]:
    dfcols = set(df.columns)
    colset = set(table_columns)

    for cols in unique_indexes:
        if "id" in [str(x).lower() for x in cols]:
            continue
        if set(cols).issubset(dfcols):
            return cols

    if interval == 1:
        if {"symbol", "datetime"}.issubset(colset) and {"symbol", "datetime"}.issubset(dfcols):
            return ["symbol", "datetime"]

    if {"symbol", "date", "time_range"}.issubset(colset) and {"symbol", "date", "time_range"}.issubset(dfcols):
        return ["symbol", "date", "time_range"]

    if {"symbol", "date", "end_time"}.issubset(colset) and {"symbol", "date", "end_time"}.issubset(dfcols):
        return ["symbol", "date", "end_time"]

    if {"symbol", "date", "time"}.issubset(colset) and {"symbol", "date", "time"}.issubset(dfcols):
        return ["symbol", "date", "time"]

    if {"symbol", "datetime"}.issubset(colset) and {"symbol", "datetime"}.issubset(dfcols):
        return ["symbol", "datetime"]

    raise ValueError(
        f"No valid conflict target found interval={interval} "
        f"table_columns={table_columns} unique_indexes={unique_indexes} df_columns={list(df.columns)}"
    )


def validate_existing_unique_index(
    table_name: str,
    preferred_conflict_target: list[str],
    unique_indexes: list[list[str]],
) -> bool:
    for cols in unique_indexes:
        if same_cols(cols, preferred_conflict_target):
            return True

    logger.warning(
        "[UPSERT] required unique index not found in live path "
        "table=%s target=%s existing_unique_indexes=%s "
        "-> NO DDL will be executed here; create the index in startup migration/bootstrap",
        table_name,
        preferred_conflict_target,
        unique_indexes,
    )
    return False