# ============================================================
# File   : trading/ranking/ranking_db_writer_lock_patch.py
# Version: PRODUCTION-STABLE-RANKING-DB-WRITER-LOCK-PATCH-V1
# ------------------------------------------------------------
# Purpose:
#   ranking_db_writer.py 本体を大きく壊さず、SQLite locked 対策を後付けする。
#
# Why:
#   ranking DB は ranking_db_writer / ranking_summary / schema ensure / AI reader が
#   同じ rankingYYYYMMDD.db を触るため、瞬間的に database is locked が起きる。
#
# Fix:
#   - DEFAULT_BUSY_TIMEOUT_MS を既定 30000ms へ延長
#   - RankingDBWriter.flush() を wrap して、失敗時に短い backoff retry
#   - 元 flush は失敗行を buffer に戻す設計なので、再flushで保存を追いつかせる
#   - 既に明示ENV指定があればそちらを優先
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(v)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_locked_error(err: Any) -> bool:
    s = str(err or "").lower()
    return "database is locked" in s or "database table is locked" in s or "locked" in s


def _buffer_count(writer: Any) -> int:
    try:
        return (
            len(getattr(writer, "raw_buffer", []) or [])
            + len(getattr(writer, "snapshot_buffer", []) or [])
            + len(getattr(writer, "legacy_buffer", []) or [])
        )
    except Exception:
        return 0


def install_ranking_db_writer_lock_patch() -> bool:
    try:
        from trading.ranking import ranking_db_writer as target
    except Exception:
        logger.exception("[RANKING DB WRITER LOCK PATCH] import target failed")
        return False

    cls = getattr(target, "RankingDBWriter", None)
    if cls is None:
        logger.warning("[RANKING DB WRITER LOCK PATCH] RankingDBWriter not found")
        return False

    if getattr(cls, "_lock_retry_patch_installed", False):
        return True

    busy_timeout_ms = _env_int("RANKING_WRITER_BUSY_TIMEOUT_MS", 30000)
    retry_max = _env_int("RANKING_WRITER_LOCK_RETRY_MAX", 5)
    retry_base_sec = _env_float("RANKING_WRITER_LOCK_RETRY_BASE_SEC", 0.5)
    retry_max_sleep_sec = _env_float("RANKING_WRITER_LOCK_RETRY_MAX_SLEEP_SEC", 3.0)

    try:
        target.DEFAULT_BUSY_TIMEOUT_MS = int(busy_timeout_ms)
    except Exception:
        pass

    orig_flush = cls.flush

    def flush_with_lock_retry(self, *args, **kwargs) -> bool:
        last_exc: Exception | None = None

        for attempt in range(int(retry_max) + 1):
            try:
                try:
                    if getattr(self, "cursor", None) is not None:
                        self.cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
                except Exception:
                    pass

                ok = bool(orig_flush(self, *args, **kwargs))
                if ok:
                    if attempt > 0:
                        logger.warning(
                            "[RANKING DB WRITER LOCK PATCH] flush recovered attempt=%s buffer=%s",
                            attempt,
                            _buffer_count(self),
                        )
                    return True

                last_error = str(getattr(self, "last_error", "") or "")
                buffer_count = _buffer_count(self)

                # 元flushがFalseを返しても、bufferが戻っているならretryする。
                # locked以外でも一時的な失敗の可能性があるため、数回だけ再試行する。
                if attempt >= int(retry_max):
                    logger.warning(
                        "[RANKING DB WRITER LOCK PATCH] flush still failed after retries attempt=%s buffer=%s last_error=%s",
                        attempt,
                        buffer_count,
                        last_error,
                    )
                    return False

                sleep_sec = min(float(retry_max_sleep_sec), float(retry_base_sec) * (2 ** attempt))
                logger.warning(
                    "[RANKING DB WRITER LOCK PATCH] flush retry attempt=%s/%s sleep=%.2fs buffer=%s last_error=%s",
                    attempt + 1,
                    retry_max,
                    sleep_sec,
                    buffer_count,
                    last_error,
                )
                time.sleep(sleep_sec)

            except sqlite3.OperationalError as e:
                last_exc = e
                if not _is_locked_error(e):
                    raise

                if attempt >= int(retry_max):
                    logger.warning(
                        "[RANKING DB WRITER LOCK PATCH] locked persists after retries attempt=%s buffer=%s err=%s",
                        attempt,
                        _buffer_count(self),
                        e,
                    )
                    return False

                try:
                    if getattr(self, "conn", None) is not None:
                        self.conn.rollback()
                except Exception:
                    pass

                sleep_sec = min(float(retry_max_sleep_sec), float(retry_base_sec) * (2 ** attempt))
                logger.warning(
                    "[RANKING DB WRITER LOCK PATCH] database locked retry attempt=%s/%s sleep=%.2fs buffer=%s err=%s",
                    attempt + 1,
                    retry_max,
                    sleep_sec,
                    _buffer_count(self),
                    e,
                )
                time.sleep(sleep_sec)

        if last_exc is not None:
            logger.warning("[RANKING DB WRITER LOCK PATCH] flush gave up err=%s", last_exc)
        return False

    cls.flush = flush_with_lock_retry
    cls._lock_retry_patch_installed = True

    logger.warning(
        "[RANKING DB WRITER LOCK PATCH] installed busy_timeout_ms=%s retry_max=%s base=%.2fs max_sleep=%.2fs",
        busy_timeout_ms,
        retry_max,
        retry_base_sec,
        retry_max_sleep_sec,
    )
    return True


try:
    install_ranking_db_writer_lock_patch()
except Exception:
    logger.exception("[RANKING DB WRITER LOCK PATCH] auto install failed")


__all__ = ["install_ranking_db_writer_lock_patch"]
