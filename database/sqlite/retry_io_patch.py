# ============================================================
# File   : database/sqlite/retry_io_patch.py
# Version: PRODUCTION-STABLE-SQLITE-NAS-IO-PATCH-V1
# ------------------------------------------------------------
# Purpose:
#   database.sqlite.retry.py 本体を壊さず、NAS SQLite の一時I/O障害を吸収する。
#
# Handles:
#   - sqlite3.OperationalError: disk I/O error
#   - sqlite3.OperationalError: unable to open database file
#   - SQLAlchemy connection pool が壊れた状態
#   - BEGIN IMMEDIATE で落ちるケース
#
# Policy:
#   - lock は短めbackoff
#   - NAS I/O は engine.dispose() して長めbackoff
#   - final失敗時は従来通り例外を上げる
# ============================================================

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

DEFAULT_BUSY_TIMEOUT_MS = int(
    os.environ.get(
        "SQLITE_BUSY_TIMEOUT_MS",
        os.environ.get("SUMMARY_UPSERT_BUSY_TIMEOUT_MS", "30000"),
    )
)
LOCK_BACKOFF_MAX_SEC = float(
    os.environ.get(
        "SQLITE_LOCK_BACKOFF_MAX_SEC",
        os.environ.get("SUMMARY_UPSERT_LOCK_BACKOFF_MAX_SEC", "4.0"),
    )
)
IO_BACKOFF_MAX_SEC = float(
    os.environ.get(
        "SQLITE_IO_BACKOFF_MAX_SEC",
        os.environ.get("SUMMARY_UPSERT_IO_BACKOFF_MAX_SEC", "10.0"),
    )
)
USE_BEGIN_IMMEDIATE_DEFAULT = os.environ.get(
    "SQLITE_USE_BEGIN_IMMEDIATE",
    os.environ.get("SUMMARY_UPSERT_BEGIN_IMMEDIATE", "1"),
).strip().lower() not in {"0", "false", "no", "off"}


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


def is_transient_io_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "disk i/o error" in msg
        or "unable to open database file" in msg
        or "i/o error" in msg
        or "input/output error" in msg
        or "the specified network name is no longer available" in msg
        or "the network path was not found" in msg
        or "network path" in msg
        or "winerror 59" in msg
        or "winerror 64" in msg
        or "winerror 121" in msg
        or "winerror 10054" in msg
    )


def lock_sleep_seconds(attempt: int, sleep_base: float = 0.45) -> float:
    base = max(0.05, float(sleep_base or 0.45))
    exp = min(6, max(0, int(attempt) - 1))
    jitter = random.random() * min(0.35, base)
    return min(LOCK_BACKOFF_MAX_SEC, base * (1.55 ** exp) + jitter)


def io_sleep_seconds(attempt: int, sleep_base: float = 0.75) -> float:
    base = max(0.20, float(sleep_base or 0.75))
    exp = min(6, max(0, int(attempt) - 1))
    jitter = random.random() * min(0.55, base)
    return min(IO_BACKOFF_MAX_SEC, base * (1.85 ** exp) + jitter)


def normal_sleep_seconds(sleep_base: float = 0.45) -> float:
    base = max(0.05, float(sleep_base or 0.45))
    return base + random.random() * 0.20


def dispose_engine_safely(engine: Any, *, reason: str = "") -> None:
    try:
        dispose = getattr(engine, "dispose", None)
        if callable(dispose):
            dispose()
            logger.warning("[SQLITE IO PATCH] engine disposed reason=%s", reason)
    except Exception:
        logger.debug("[SQLITE IO PATCH] engine dispose failed reason=%s", reason, exc_info=True)


def prepare_sqlite_connection(conn: Any, *, busy_timeout_ms: Optional[int] = None) -> None:
    timeout_ms = int(busy_timeout_ms or DEFAULT_BUSY_TIMEOUT_MS)

    try:
        conn.execute(text(f"PRAGMA busy_timeout={timeout_ms}"))
    except Exception:
        logger.debug("[SQLITE IO PATCH] PRAGMA busy_timeout failed", exc_info=True)

    try:
        conn.execute(text("PRAGMA synchronous=NORMAL"))
    except Exception:
        logger.debug("[SQLITE IO PATCH] PRAGMA synchronous failed", exc_info=True)


def run_sql_many_with_retry(
    *,
    engine: Any,
    sql: str,
    params: list[dict],
    log_prefix: str,
    interval: int | str = "",
    table_name: str = "",
    chunk_no: int = 1,
    total_chunks: int = 1,
    retry: int = 12,
    sleep_base: float = 0.45,
    use_begin_immediate: Optional[bool] = None,
) -> None:
    if not params:
        logger.info(
            "%s skipped empty params interval=%s table=%s chunk=%s/%s",
            log_prefix,
            interval,
            table_name,
            chunk_no,
            total_chunks,
        )
        return

    # NAS一時障害は12回では足りないことがあるため、最低18回に底上げ。
    retry = max(18, int(retry or 1))
    last_err: Exception | None = None
    use_immediate = USE_BEGIN_IMMEDIATE_DEFAULT if use_begin_immediate is None else bool(use_begin_immediate)

    for attempt in range(1, retry + 1):
        try:
            with engine.connect() as conn:
                prepare_sqlite_connection(conn)

                if use_immediate:
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
            is_io = is_transient_io_error(e)

            if is_io:
                dispose_engine_safely(engine, reason="transient_io_error")

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
                time.sleep(sleep_s)
                continue

            if is_io and attempt < retry:
                sleep_s = io_sleep_seconds(attempt, max(0.75, sleep_base))
                logger.warning(
                    "%s transient-io retry interval=%s table=%s chunk=%s/%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
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


def install_retry_io_patch() -> bool:
    try:
        from database.sqlite import retry as retry_mod

        retry_mod.DEFAULT_BUSY_TIMEOUT_MS = DEFAULT_BUSY_TIMEOUT_MS
        retry_mod.is_lock_error = is_lock_error
        retry_mod.lock_sleep_seconds = lock_sleep_seconds
        retry_mod.normal_sleep_seconds = normal_sleep_seconds
        retry_mod.prepare_sqlite_connection = prepare_sqlite_connection
        retry_mod.run_sql_many_with_retry = run_sql_many_with_retry

        logger.warning(
            "[SQLITE IO PATCH] installed busy_timeout_ms=%s io_backoff_max=%.1fs retry_min=18",
            DEFAULT_BUSY_TIMEOUT_MS,
            IO_BACKOFF_MAX_SEC,
        )
        return True
    except Exception:
        logger.exception("[SQLITE IO PATCH] install failed")
        return False


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "is_lock_error",
    "is_transient_io_error",
    "lock_sleep_seconds",
    "io_sleep_seconds",
    "normal_sleep_seconds",
    "dispose_engine_safely",
    "prepare_sqlite_connection",
    "run_sql_many_with_retry",
    "install_retry_io_patch",
]
