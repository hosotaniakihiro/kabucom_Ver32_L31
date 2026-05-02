# ============================================================
# File   : trading/summary/persistence/core/sqlite_retry.py
# Version: PRODUCTION-STABLE-REV1.0-NAS-SQLITE-LOCK-RETRY
# ------------------------------------------------------------
# Purpose:
#   SQLite database is locked 対策。
# ============================================================

from __future__ import annotations

import logging
import os
import random
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

DEFAULT_BUSY_TIMEOUT_MS = int(os.environ.get("SUMMARY_UPSERT_BUSY_TIMEOUT_MS", "15000"))
LOCK_BACKOFF_MAX_SEC = float(os.environ.get("SUMMARY_UPSERT_LOCK_BACKOFF_MAX_SEC", "4.0"))
USE_BEGIN_IMMEDIATE = os.environ.get("SUMMARY_UPSERT_BEGIN_IMMEDIATE", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


def is_lock_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "database is locked" in msg
        or "database table is locked" in msg
        or "database schema is locked" in msg
        or "sqlite_busy" in msg
        or "database is busy" in msg
        or ("locked" in msg and "sqlite" in msg)
    )


def lock_sleep_seconds(attempt: int, sleep_base: float) -> float:
    base = max(0.05, float(sleep_base or 0.45))
    exp = min(6, max(0, int(attempt) - 1))
    jitter = random.random() * min(0.35, base)
    return min(LOCK_BACKOFF_MAX_SEC, base * (1.55 ** exp) + jitter)


def normal_sleep_seconds(sleep_base: float) -> float:
    base = max(0.05, float(sleep_base or 0.45))
    return base + random.random() * 0.20


def prepare_sqlite_connection(conn: Any) -> None:
    """
    SQLAlchemy Connection に SQLite PRAGMA を設定する。

    注意:
      - busy_timeout は connection ごとに必要
      - NAS SQLite では journal_mode は触らない
    """
    try:
        conn.execute(text(f"PRAGMA busy_timeout={int(DEFAULT_BUSY_TIMEOUT_MS)}"))
    except Exception:
        logger.debug("[UPSERT] PRAGMA busy_timeout failed", exc_info=True)

    try:
        conn.execute(text("PRAGMA synchronous=NORMAL"))
    except Exception:
        logger.debug("[UPSERT] PRAGMA synchronous failed", exc_info=True)


def run_sql_many_with_retry(
    *,
    engine: Any,
    sql: str,
    params: list[dict],
    log_prefix: str,
    interval: int,
    table_name: str,
    chunk_no: int,
    total_chunks: int,
    retry: int,
    sleep_base: float,
) -> None:
    """
    executemany を lock retry 付きで実行する。
    """
    last_err: Exception | None = None
    retry = max(1, int(retry or 1))

    for attempt in range(1, retry + 1):
        try:
            with engine.connect() as conn:
                prepare_sqlite_connection(conn)

                if USE_BEGIN_IMMEDIATE:
                    begun = False
                    try:
                        conn.execute(text("BEGIN IMMEDIATE"))
                        begun = True
                        conn.execute(text(sql), params)
                        conn.execute(text("COMMIT"))
                    except Exception:
                        try:
                            if begun:
                                conn.execute(text("ROLLBACK"))
                        except Exception:
                            pass
                        raise
                else:
                    with conn.begin():
                        conn.execute(text(sql), params)

            logger.info(
                "%s ok interval=%s table=%s chunk=%s/%s rows=%s attempt=%s",
                log_prefix,
                interval,
                table_name,
                chunk_no,
                total_chunks,
                len(params),
                attempt,
            )
            return

        except Exception as e:
            last_err = e
            is_lock = is_lock_error(e)

            if is_lock and attempt < retry:
                sleep_s = lock_sleep_seconds(attempt, sleep_base)
                logger.warning(
                    "%s locked retry interval=%s table=%s chunk=%s/%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
                    log_prefix,
                    interval,
                    table_name,
                    chunk_no,
                    total_chunks,
                    len(params),
                    attempt,
                    retry,
                    sleep_s,
                    str(e).splitlines()[0] if str(e) else type(e).__name__,
                )
                import time
                time.sleep(sleep_s)
                continue

            if attempt < retry:
                sleep_s = normal_sleep_seconds(sleep_base)
                logger.warning(
                    "%s failed retry interval=%s table=%s chunk=%s/%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
                    log_prefix,
                    interval,
                    table_name,
                    chunk_no,
                    total_chunks,
                    len(params),
                    attempt,
                    retry,
                    sleep_s,
                    str(e).splitlines()[0] if str(e) else type(e).__name__,
                    exc_info=True,
                )
                import time
                time.sleep(sleep_s)
                continue

            logger.exception(
                "%s failed final interval=%s table=%s chunk=%s/%s rows=%s attempt=%s/%s",
                log_prefix,
                interval,
                table_name,
                chunk_no,
                total_chunks,
                len(params),
                attempt,
                retry,
            )
            break

    if last_err is not None:
        raise last_err