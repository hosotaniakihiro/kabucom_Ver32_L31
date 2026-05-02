# ============================================================
# File   : trading/summary/persistence/schema_constraints.py
# Ver    : PRODUCTION-STABLE-SUMMARY-CONSTRAINT-GUARD-V1
# ------------------------------------------------------------
# ✔ stock_summary_* テーブルの UNIQUE(symbol, datetime) を自動補修
# ✔ 既存重複データがある場合は rowid ベースで重複削除
# ✔ SQLite 向け安全運用
# ✔ ON CONFLICT(symbol, datetime) を成立させる
# ============================================================

from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_DONE_TABLES: set[str] = set()

SUMMARY_TABLES_DEFAULT = (
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
    "stock_summary_10min",
    "stock_summary_15min",
    "stock_summary_30min",
    "stock_summary_60min",
    "stock_summary_daily",
)


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        text(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name=:table_name
            LIMIT 1
            """
        ),
        {"table_name": table_name},
    ).fetchone()
    return row is not None


def _get_index_names(conn, table_name: str) -> list[str]:
    rows = conn.execute(text(f"PRAGMA index_list('{table_name}')")).fetchall()
    result = []
    for row in rows:
        try:
            # sqlite pragma index_list:
            # seq, name, unique, origin, partial
            result.append(str(row[1]))
        except Exception:
            pass
    return result


def _index_has_symbol_datetime(conn, index_name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA index_info('{index_name}')")).fetchall()
    cols = []
    for row in rows:
        try:
            cols.append(str(row[2]))
        except Exception:
            pass
    return cols == ["symbol", "datetime"]


def _has_unique_symbol_datetime(conn, table_name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA index_list('{table_name}')")).fetchall()
    for row in rows:
        try:
            is_unique = int(row[2]) == 1
            index_name = str(row[1])
        except Exception:
            continue
        if not is_unique:
            continue
        if _index_has_symbol_datetime(conn, index_name):
            return True
    return False


def _delete_duplicate_symbol_datetime_rows(conn, table_name: str) -> int:
    """
    rowid が最大の行を残し、それ以外を削除
    """
    dup_count_row = conn.execute(
        text(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT symbol, datetime, COUNT(*) AS c
                FROM {table_name}
                GROUP BY symbol, datetime
                HAVING COUNT(*) > 1
            ) t
            """
        )
    ).fetchone()

    dup_groups = int(dup_count_row[0] or 0) if dup_count_row else 0
    if dup_groups <= 0:
        return 0

    logger.warning(
        "[SCHEMA GUARD] duplicate (symbol, datetime) found table=%s groups=%s -> dedupe start",
        table_name,
        dup_groups,
    )

    before_changes = conn.execute(text("SELECT total_changes()")).scalar() or 0

    conn.execute(
        text(
            f"""
            DELETE FROM {table_name}
            WHERE rowid NOT IN (
                SELECT MAX(rowid)
                FROM {table_name}
                GROUP BY symbol, datetime
            )
            """
        )
    )

    after_changes = conn.execute(text("SELECT total_changes()")).scalar() or 0
    deleted = int(after_changes - before_changes)

    logger.warning(
        "[SCHEMA GUARD] duplicate rows deleted table=%s deleted=%s",
        table_name,
        deleted,
    )
    return deleted


def _create_unique_index(conn, table_name: str) -> str:
    index_name = f"ux_{table_name}_symbol_datetime"
    conn.execute(
        text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
            ON {table_name}(symbol, datetime)
            """
        )
    )
    return index_name


def ensure_unique_constraint_for_table(engine, table_name: str) -> bool:
    """
    SQLite の ON CONFLICT(symbol, datetime) を成立させるため、
    UNIQUE INDEX を補修する。
    """
    if engine is None:
        logger.warning(
            "[SCHEMA GUARD] engine is None table=%s",
            table_name,
        )
        return False

    with _LOCK:
        if table_name in _DONE_TABLES:
            return True

        try:
            with engine.begin() as conn:
                if not _table_exists(conn, table_name):
                    logger.info(
                        "[SCHEMA GUARD] table not found skip table=%s",
                        table_name,
                    )
                    _DONE_TABLES.add(table_name)
                    return False

                if _has_unique_symbol_datetime(conn, table_name):
                    logger.info(
                        "[SCHEMA GUARD] unique(symbol, datetime) already exists table=%s",
                        table_name,
                    )
                    _DONE_TABLES.add(table_name)
                    return True

                _delete_duplicate_symbol_datetime_rows(conn, table_name)
                index_name = _create_unique_index(conn, table_name)

                if not _has_unique_symbol_datetime(conn, table_name):
                    raise RuntimeError(
                        f"unique index create verification failed: table={table_name}"
                    )

                logger.info(
                    "[SCHEMA GUARD] unique(symbol, datetime) created table=%s index=%s",
                    table_name,
                    index_name,
                )

                _DONE_TABLES.add(table_name)
                return True

        except Exception:
            logger.exception(
                "[SCHEMA GUARD] ensure unique constraint failed table=%s",
                table_name,
            )
            return False


def ensure_summary_unique_constraints(
    engine,
    tables: Optional[Iterable[str]] = None,
) -> dict[str, bool]:
    tables = tuple(tables or SUMMARY_TABLES_DEFAULT)
    result: dict[str, bool] = {}
    for table_name in tables:
        result[table_name] = ensure_unique_constraint_for_table(engine, table_name)
    return result


def ensure_summary_unique_constraint_by_interval(engine, interval: int | str) -> bool:
    try:
        iv = str(int(interval))
    except Exception:
        iv = str(interval).strip()

    mapping = {
        "1": "stock_summary_1min",
        "3": "stock_summary_3min",
        "5": "stock_summary_5min",
        "10": "stock_summary_10min",
        "15": "stock_summary_15min",
        "30": "stock_summary_30min",
        "60": "stock_summary_60min",
        "daily": "stock_summary_daily",
    }

    table_name = mapping.get(iv)
    if not table_name:
        logger.warning(
            "[SCHEMA GUARD] unknown interval=%s",
            interval,
        )
        return False

    return ensure_unique_constraint_for_table(engine, table_name)