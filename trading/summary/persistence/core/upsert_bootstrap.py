# ============================================================
# File   : trading/summary/persistence/core/upsert_bootstrap.py
# Version: Ver1.1-PRODUCTION-UPSERT-BOOTSTRAP-BEST-EFFORT
# ------------------------------------------------------------
# ✔ 起動時に1回だけ summary table の UNIQUE index を整備
# ✔ live path では呼ばない前提
# ✔ 既存重複がある場合は keep=MAX(rowid) で整理
# ✔ SQLite database is locked を retry
# ✔ lock が解消しない場合は raise せず warning + skip
# ✔ 1min / 3min / 5min を優先、他 interval にも拡張可能
# ============================================================

from __future__ import annotations

import logging
import random
import time
from typing import Iterable

from sqlalchemy import text

from .upsert_metadata import (
    apply_sqlite_session_pragmas,
    fetch_table_columns,
    fetch_unique_indexes,
    get_summary_engine,
    make_unique_index_name,
    quote_ident,
    resolve_table_name,
    same_cols,
)

logger = logging.getLogger(__name__)

BOOTSTRAP_LOCK_RETRY_COUNT = 8
BOOTSTRAP_LOCK_RETRY_BASE_SLEEP_SEC = 0.30
BOOTSTRAP_LOCK_RETRY_MAX_SLEEP_SEC = 2.50


def _is_locked_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return (
        "database is locked" in s
        or "database table is locked" in s
        or "sqlite busy" in s
    )


def _retry_sleep_sec(attempt_no: int) -> float:
    base = BOOTSTRAP_LOCK_RETRY_BASE_SLEEP_SEC * (2 ** max(0, attempt_no - 1))
    base = min(base, BOOTSTRAP_LOCK_RETRY_MAX_SLEEP_SEC)
    jitter = random.uniform(0.0, 0.15)
    return base + jitter


def _choose_bootstrap_target(table_columns: list[str], interval: int) -> list[str]:
    colset = set(table_columns)

    if int(interval) == 1 and {"symbol", "datetime"}.issubset(colset):
        return ["symbol", "datetime"]

    if {"symbol", "date", "time_range"}.issubset(colset):
        return ["symbol", "date", "time_range"]

    if {"symbol", "date", "end_time"}.issubset(colset):
        return ["symbol", "date", "end_time"]

    if {"symbol", "date", "time"}.issubset(colset):
        return ["symbol", "date", "time"]

    if {"symbol", "datetime"}.issubset(colset):
        return ["symbol", "datetime"]

    return []


def _cleanup_existing_table_duplicates(conn, table_name: str, target_cols: list[str]) -> int:
    if not target_cols:
        return 0

    quoted_table = quote_ident(table_name)
    qcols = [quote_ident(c) for c in target_cols]

    not_null_cond = " AND ".join([f"{c} IS NOT NULL" for c in qcols])
    group_cols = ", ".join(qcols)
    join_eq = " AND ".join([f"t.{c} = k.{c}" for c in qcols])

    sql_count = f"""
    SELECT COUNT(*) AS cnt
    FROM (
        SELECT {group_cols}, COUNT(*) AS c
        FROM {quoted_table}
        WHERE {not_null_cond}
        GROUP BY {group_cols}
        HAVING COUNT(*) > 1
    ) z
    """

    sql_delete = f"""
    DELETE FROM {quoted_table}
    WHERE rowid IN (
        SELECT t.rowid
        FROM {quoted_table} t
        JOIN (
            SELECT {group_cols}, MAX(rowid) AS keep_rowid
            FROM {quoted_table}
            WHERE {not_null_cond}
            GROUP BY {group_cols}
            HAVING COUNT(*) > 1
        ) k
          ON {join_eq}
        WHERE t.rowid <> k.keep_rowid
    )
    """

    dup_groups = int(conn.execute(text(sql_count)).scalar() or 0)
    if dup_groups <= 0:
        return 0

    result = conn.execute(text(sql_delete))
    removed = int(getattr(result, "rowcount", 0) or 0)

    logger.warning(
        "[UPSERT BOOTSTRAP] duplicates cleaned table=%s target=%s dup_groups=%s removed=%s",
        table_name,
        target_cols,
        dup_groups,
        removed,
    )
    return removed


def _has_unique_index(conn, table_name: str, target_cols: list[str]) -> bool:
    unique_indexes = fetch_unique_indexes(conn, table_name)
    for cols in unique_indexes:
        if same_cols(cols, target_cols):
            return True
    return False


def _ensure_unique_index_once(conn, table_name: str, target_cols: list[str]) -> bool:
    if not target_cols:
        logger.warning(
            "[UPSERT BOOTSTRAP] skip unique index ensure table=%s reason=no target cols",
            table_name,
        )
        return False

    if _has_unique_index(conn, table_name, target_cols):
        logger.info(
            "[UPSERT BOOTSTRAP] unique index already exists table=%s target=%s",
            table_name,
            target_cols,
        )
        return True

    idx_name = make_unique_index_name(table_name, target_cols)
    quoted_table = quote_ident(table_name)
    quoted_cols = ", ".join(quote_ident(c) for c in target_cols)

    create_sql = (
        f'CREATE UNIQUE INDEX IF NOT EXISTS {quote_ident(idx_name)} '
        f'ON {quoted_table} ({quoted_cols})'
    )

    try:
        conn.execute(text(create_sql))
        logger.info(
            "[UPSERT BOOTSTRAP] unique index created table=%s index=%s target=%s",
            table_name,
            idx_name,
            target_cols,
        )
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "unique constraint failed" not in msg:
            raise

    removed = _cleanup_existing_table_duplicates(conn, table_name, target_cols)

    conn.execute(text(create_sql))
    logger.warning(
        "[UPSERT BOOTSTRAP] unique index created after cleanup table=%s index=%s target=%s removed=%s",
        table_name,
        idx_name,
        target_cols,
        removed,
    )
    return True


def _ensure_table_unique_index_with_retry(engine, table_name: str, interval: int) -> bool:
    last_exc: Exception | None = None

    for attempt in range(1, BOOTSTRAP_LOCK_RETRY_COUNT + 1):
        try:
            with engine.begin() as conn:
                apply_sqlite_session_pragmas(conn)

                table_columns = fetch_table_columns(conn, table_name)
                if not table_columns:
                    logger.warning(
                        "[UPSERT BOOTSTRAP] table columns not found table=%s",
                        table_name,
                    )
                    return False

                target_cols = _choose_bootstrap_target(table_columns, interval)
                if not target_cols:
                    logger.warning(
                        "[UPSERT BOOTSTRAP] no bootstrap target found table=%s interval=%s columns=%s",
                        table_name,
                        interval,
                        table_columns,
                    )
                    return False

                return _ensure_unique_index_once(conn, table_name, target_cols)

        except Exception as exc:
            last_exc = exc

            if _is_locked_error(exc) and attempt < BOOTSTRAP_LOCK_RETRY_COUNT:
                sleep_sec = _retry_sleep_sec(attempt)
                logger.warning(
                    "[UPSERT BOOTSTRAP] retry table=%s interval=%s attempt=%s sleep=%.3fs err=%s",
                    table_name,
                    interval,
                    attempt,
                    sleep_sec,
                    exc,
                )
                time.sleep(sleep_sec)
                continue

            if _is_locked_error(exc):
                logger.warning(
                    "[UPSERT BOOTSTRAP] skip locked table=%s interval=%s attempts=%s err=%s",
                    table_name,
                    interval,
                    attempt,
                    exc,
                )
                return False

            logger.exception(
                "[UPSERT BOOTSTRAP] failed table=%s interval=%s attempt=%s",
                table_name,
                interval,
                attempt,
            )
            return False

    if last_exc is not None and _is_locked_error(last_exc):
        logger.warning(
            "[UPSERT BOOTSTRAP] skip locked table=%s interval=%s after retries err=%s",
            table_name,
            interval,
            last_exc,
        )
        return False

    return False


def bootstrap_summary_unique_indexes(intervals: Iterable[int] = (1, 3, 5)) -> dict[int, bool]:
    """
    起動時に1回だけ summary系テーブルの UNIQUE index を整備する。
    lock が取れない場合は False を返して継続する。

    Returns:
        {interval: success_bool}
    """
    engine = get_summary_engine()
    results: dict[int, bool] = {}

    for interval in intervals:
        interval = int(interval)
        table_name = resolve_table_name(interval)

        try:
            ok = _ensure_table_unique_index_with_retry(
                engine=engine,
                table_name=table_name,
                interval=interval,
            )
            results[interval] = bool(ok)
        except Exception:
            logger.exception(
                "[UPSERT BOOTSTRAP] ensure failed interval=%s table=%s",
                interval,
                table_name,
            )
            results[interval] = False

    logger.info("[UPSERT BOOTSTRAP] done results=%s", results)
    return results


def bootstrap_summary_unique_indexes_extended() -> dict[int, bool]:
    return bootstrap_summary_unique_indexes(
        intervals=(1, 3, 5, 10, 15, 30, 60, 1440)
    )