# ============================================================
# File   : database/sqlite/inspector.py
# Version: PRODUCTION-STABLE-REV1.0-SQLITE-INSPECTOR
# ------------------------------------------------------------
# Purpose:
#   SQLite PRAGMA table_info / index_list / unique constraint inspection
# ============================================================

from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .retry import prepare_sqlite_connection
from .sql_builder import quote_ident, sqlite_quote_literal

logger = logging.getLogger(__name__)

_CONSTRAINT_CACHE_LOCK = threading.RLock()
_CONSTRAINT_CACHE: Dict[Tuple[int, str, Tuple[str, ...]], bool] = {}

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
        logger.exception("[SQLITE INSPECTOR] table column inspect failed table=%s", table_name)
        cols = []

    with _TABLE_COLUMNS_CACHE_LOCK:
        _TABLE_COLUMNS_CACHE[cache_key] = list(cols)

    logger.info("[SQLITE INSPECTOR] table columns inspected table=%s cols=%s", table_name, cols)
    return cols


def invalidate_table_columns_cache(table_name: Optional[str] = None) -> None:
    with _TABLE_COLUMNS_CACHE_LOCK:
        if table_name is None:
            _TABLE_COLUMNS_CACHE.clear()
            logger.info("[SQLITE INSPECTOR] table columns cache cleared all")
            return

        delete_keys = [k for k in _TABLE_COLUMNS_CACHE.keys() if k[1] == table_name]
        for k in delete_keys:
            _TABLE_COLUMNS_CACHE.pop(k, None)

        logger.info("[SQLITE INSPECTOR] table columns cache cleared table=%s", table_name)


def read_index_columns(conn, index_name: str) -> List[str]:
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
        logger.exception("[SQLITE INSPECTOR] PRAGMA index_info failed index=%s", index_name)
        return []


def table_has_unique_constraint(engine: Engine, table_name: str, columns: Iterable[str]) -> bool:
    target_cols = tuple(str(c) for c in columns)
    cache_key = (id(engine), table_name, target_cols)

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

                if tuple(pk_cols) == target_cols or set(pk_cols) == set(target_cols):
                    ok = True

            except Exception:
                logger.exception("[SQLITE INSPECTOR] PRAGMA table_info failed table=%s", table_name)

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

                        idx_cols = read_index_columns(conn, idx_name)
                        if tuple(idx_cols) == target_cols or set(idx_cols) == set(target_cols):
                            ok = True
                            break

                except Exception:
                    logger.exception("[SQLITE INSPECTOR] PRAGMA index_list failed table=%s", table_name)

    except Exception:
        logger.exception("[SQLITE INSPECTOR] constraint inspection failed table=%s", table_name)
        ok = False

    with _CONSTRAINT_CACHE_LOCK:
        _CONSTRAINT_CACHE[cache_key] = ok

    logger.info(
        "[SQLITE INSPECTOR] unique constraint inspect table=%s columns=%s ok=%s",
        table_name,
        target_cols,
        ok,
    )
    return ok


def invalidate_constraint_cache(table_name: Optional[str] = None) -> None:
    with _CONSTRAINT_CACHE_LOCK:
        if table_name is None:
            _CONSTRAINT_CACHE.clear()
            logger.info("[SQLITE INSPECTOR] constraint cache cleared all")
            return

        delete_keys = [k for k in _CONSTRAINT_CACHE.keys() if k[1] == table_name]
        for k in delete_keys:
            _CONSTRAINT_CACHE.pop(k, None)

        logger.info("[SQLITE INSPECTOR] constraint cache cleared table=%s", table_name)
