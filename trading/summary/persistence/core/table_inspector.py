# ============================================================
# File   : trading/summary/persistence/core/table_inspector.py
# Version: PRODUCTION-STABLE-REV1.0
# ------------------------------------------------------------
# Purpose:
#   SQLite table columns / UNIQUE INDEX inspection
# ============================================================

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .chunk_utils import valid_conflict_key_rows
from .sql_builder import quote_ident, sqlite_quote_literal
from .sqlite_retry import prepare_sqlite_connection

logger = logging.getLogger(__name__)

_CONSTRAINT_CACHE_LOCK = threading.RLock()
_CONSTRAINT_CACHE: Dict[Tuple[int, str], bool] = {}

_TABLE_COLUMNS_CACHE_LOCK = threading.RLock()
_TABLE_COLUMNS_CACHE: Dict[Tuple[int, str], List[str]] = {}


def read_table_columns(engine: Engine, table_name: str) -> List[str]:
    cache_key = (id(engine), table_name)

    with _TABLE_COLUMNS_CACHE_LOCK:
        if cache_key in _TABLE_COLUMNS_CACHE:
            return list(_TABLE_COLUMNS_CACHE[cache_key])

    cols: List[str] = []

    try:
        with engine.connect() as conn:
            prepare_sqlite_connection(conn)
            rs = conn.execute(text(f"PRAGMA table_info({quote_ident(table_name)})"))
            for row in rs:
                try:
                    cols.append(str(row[1]))
                except Exception:
                    try:
                        cols.append(str(row["name"]))
                    except Exception:
                        pass
    except Exception:
        logger.exception("[UPSERT] table column inspect failed table=%s", table_name)
        cols = []

    with _TABLE_COLUMNS_CACHE_LOCK:
        _TABLE_COLUMNS_CACHE[cache_key] = list(cols)

    logger.info("[UPSERT] table columns inspected table=%s cols=%s", table_name, cols)
    return cols


def invalidate_table_columns_cache(table_name: Optional[str] = None) -> None:
    with _TABLE_COLUMNS_CACHE_LOCK:
        if table_name is None:
            _TABLE_COLUMNS_CACHE.clear()
            logger.info("[UPSERT] table columns cache cleared all")
            return

        delete_keys = [k for k in _TABLE_COLUMNS_CACHE.keys() if k[1] == table_name]
        for k in delete_keys:
            _TABLE_COLUMNS_CACHE.pop(k, None)

        logger.info("[UPSERT] table columns cache cleared table=%s", table_name)


def filter_rows_to_existing_columns(
    engine: Engine,
    table_name: str,
    rows: Sequence[dict],
) -> List[dict]:
    if not rows:
        return []

    table_cols = set(read_table_columns(engine, table_name))
    if not table_cols:
        logger.warning("[UPSERT] no table columns found -> skip filtering table=%s", table_name)
        return [dict(r) for r in rows]

    out: List[dict] = []
    dropped_cols_logged = set()

    for row in rows:
        nr = {}
        for k, v in row.items():
            if k in table_cols:
                nr[k] = v
            else:
                if k not in dropped_cols_logged:
                    dropped_cols_logged.add(k)
                    logger.warning("[UPSERT] dropping unknown column table=%s column=%s", table_name, k)

        if nr:
            out.append(nr)

    filtered = valid_conflict_key_rows(out)
    dropped_rows = len(out) - len(filtered)

    if dropped_rows > 0:
        logger.warning(
            "[UPSERT] rows dropped after column filter table=%s dropped_rows=%s",
            table_name,
            dropped_rows,
        )

    return filtered


def _read_index_columns(conn, index_name: str) -> List[str]:
    try:
        rs = conn.execute(text(f"PRAGMA index_info({sqlite_quote_literal(index_name)})"))
        cols = []
        for row in rs:
            try:
                cols.append(str(row[2]))
            except Exception:
                try:
                    cols.append(str(row["name"]))
                except Exception:
                    pass
        return cols
    except Exception:
        logger.exception("[UPSERT] PRAGMA index_info failed index=%s", index_name)
        return []


def table_has_symbol_datetime_unique_constraint(engine: Engine, table_name: str) -> bool:
    cache_key = (id(engine), table_name)

    with _CONSTRAINT_CACHE_LOCK:
        if cache_key in _CONSTRAINT_CACHE:
            return _CONSTRAINT_CACHE[cache_key]

    ok = False

    try:
        with engine.connect() as conn:
            prepare_sqlite_connection(conn)

            try:
                rs = conn.execute(text(f"PRAGMA table_info({quote_ident(table_name)})"))
                pk_cols: List[str] = []
                for row in rs:
                    try:
                        name = str(row[1])
                        pk_ord = int(row[5] or 0)
                    except Exception:
                        name = str(row["name"])
                        pk_ord = int(row["pk"] or 0)

                    if pk_ord > 0:
                        pk_cols.append(name)

                if pk_cols == ["symbol", "datetime"] or pk_cols == ["datetime", "symbol"]:
                    ok = True

            except Exception:
                logger.exception("[UPSERT] PRAGMA table_info failed table=%s", table_name)

            if not ok:
                try:
                    rs = conn.execute(text(f"PRAGMA index_list({quote_ident(table_name)})"))
                    for row in rs:
                        try:
                            idx_name = str(row[1])
                            is_unique = int(row[2] or 0)
                        except Exception:
                            idx_name = str(row["name"])
                            is_unique = int(row["unique"] or 0)

                        if not is_unique:
                            continue

                        idx_cols = _read_index_columns(conn, idx_name)
                        if idx_cols == ["symbol", "datetime"] or idx_cols == ["datetime", "symbol"]:
                            ok = True
                            break

                except Exception:
                    logger.exception("[UPSERT] PRAGMA index_list failed table=%s", table_name)

    except Exception:
        logger.exception("[UPSERT] constraint inspection failed table=%s", table_name)
        ok = False

    with _CONSTRAINT_CACHE_LOCK:
        _CONSTRAINT_CACHE[cache_key] = ok

    logger.info("[UPSERT] constraint inspect table=%s has_symbol_datetime_unique=%s", table_name, ok)
    return ok


def invalidate_constraint_cache(table_name: Optional[str] = None) -> None:
    with _CONSTRAINT_CACHE_LOCK:
        if table_name is None:
            _CONSTRAINT_CACHE.clear()
            logger.info("[UPSERT] constraint cache cleared all")
            return

        delete_keys = [k for k in _CONSTRAINT_CACHE.keys() if k[1] == table_name]
        for k in delete_keys:
            _CONSTRAINT_CACHE.pop(k, None)

        logger.info("[UPSERT] constraint cache cleared table=%s", table_name)