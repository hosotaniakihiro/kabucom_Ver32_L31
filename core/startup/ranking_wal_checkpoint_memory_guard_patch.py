# ============================================================
# File   : core/startup/ranking_wal_checkpoint_memory_guard_patch.py
# Version: V1-RANKING-WAL-CHECKPOINT-MEMORY-GUARD
# ------------------------------------------------------------
# Purpose:
#   main_database.py / ranking collector can keep rankingYYYYMMDD.db-wal
#   growing during long sessions.  This patch is installed from
#   usercustomize/sitecustomize and patches RankingDBWriter at runtime.
#
# Fix:
#   - Reduce SQLite page cache for ranking writer.
#   - Prefer FILE temp store to avoid large temp memory retention.
#   - Run PASSIVE checkpoints after flush.
#   - If -wal file exceeds threshold, run TRUNCATE checkpoint.
#   - gc.collect() after large flushes to release dataframe/tuple pressure.
# ============================================================
from __future__ import annotations

import gc
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, default)).strip()))
    except Exception:
        return int(default)


def _wal_path(db_path: Any) -> Path | None:
    try:
        if not db_path:
            return None
        return Path(str(db_path) + "-wal")
    except Exception:
        return None


def _wal_mb(db_path: Any) -> float:
    try:
        p = _wal_path(db_path)
        if p and p.exists():
            return p.stat().st_size / 1024 / 1024
    except Exception:
        pass
    return 0.0


def _checkpoint(writer: Any, *, mode: str, reason: str) -> bool:
    try:
        conn = getattr(writer, "conn", None)
        cursor = getattr(writer, "cursor", None)
        if conn is None or cursor is None:
            return False
        before = _wal_mb(getattr(writer, "db_path", None))
        cursor.execute(f"PRAGMA wal_checkpoint({mode});")
        after = _wal_mb(getattr(writer, "db_path", None))
        logger.warning(
            "[RANKING WAL GUARD] checkpoint mode=%s reason=%s db=%s wal_mb %.1f -> %.1f",
            mode,
            reason,
            getattr(writer, "db_path", None),
            before,
            after,
        )
        return True
    except Exception:
        logger.debug("[RANKING WAL GUARD] checkpoint failed mode=%s reason=%s", mode, reason, exc_info=True)
        return False


def _apply() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.ranking.ranking_db_writer as mod
        cls = getattr(mod, "RankingDBWriter", None)
        if cls is None:
            return False
    except Exception:
        logger.debug("[RANKING WAL GUARD] ranking writer not ready", exc_info=True)
        return False

    try:
        if getattr(cls, "_ranking_wal_guard_v1", False):
            _INSTALLED = True
            return True

        orig_open = getattr(cls, "_open_connection")
        orig_flush = getattr(cls, "flush")
        orig_loop = getattr(cls, "_loop")

        def _open_connection_patched(self, *args: Any, **kwargs: Any):
            ret = orig_open(self, *args, **kwargs)
            try:
                cursor = getattr(self, "cursor", None)
                if cursor is not None:
                    cache_kb = _env_int("RANKING_SQLITE_CACHE_KB", -8192)
                    wal_autockpt = _env_int("RANKING_WRITER_WAL_AUTOCHECKPOINT", 200)
                    temp_store = str(os.getenv("RANKING_SQLITE_TEMP_STORE", "FILE")).strip().upper()
                    if temp_store not in {"DEFAULT", "FILE", "MEMORY"}:
                        temp_store = "FILE"
                    cursor.execute(f"PRAGMA cache_size={cache_kb};")
                    cursor.execute(f"PRAGMA wal_autocheckpoint={wal_autockpt};")
                    cursor.execute(f"PRAGMA temp_store={temp_store};")
                    logger.warning(
                        "[RANKING WAL GUARD] sqlite tuned db=%s cache_kb=%s wal_autocheckpoint=%s temp_store=%s",
                        getattr(self, "db_path", None), cache_kb, wal_autockpt, temp_store,
                    )
            except Exception:
                logger.debug("[RANKING WAL GUARD] sqlite tune failed", exc_info=True)
            return ret

        def _flush_patched(self, *args: Any, **kwargs: Any):
            ret = orig_flush(self, *args, **kwargs)
            try:
                if not ret:
                    return ret
                if _env_bool("RANKING_WRITER_PASSIVE_CHECKPOINT_AFTER_FLUSH", True):
                    _checkpoint(self, mode="PASSIVE", reason="after_flush")
                wal_mb = _wal_mb(getattr(self, "db_path", None))
                threshold = _env_float("RANKING_WRITER_WAL_TRUNCATE_MB", 128.0)
                if wal_mb >= threshold:
                    _checkpoint(self, mode="TRUNCATE", reason=f"wal_mb>={threshold:.1f}")
                if _env_bool("RANKING_WRITER_GC_AFTER_FLUSH", True):
                    gc.collect()
            except Exception:
                logger.debug("[RANKING WAL GUARD] after flush guard failed", exc_info=True)
            return ret

        def _loop_patched(self, *args: Any, **kwargs: Any):
            # Original loop handles flush. This wrapper adds idle checkpoint every N seconds.
            last_idle_checkpoint = 0.0
            idle_interval = max(5.0, _env_float("RANKING_WRITER_IDLE_CHECKPOINT_SEC", 60.0))
            stop_event = getattr(self, "_stop_event", None)
            if stop_event is None:
                return orig_loop(self, *args, **kwargs)
            logger.info("[RANKING WAL GUARD] loop wrapper started idle_checkpoint_sec=%.1f", idle_interval)
            while not stop_event.is_set():
                try:
                    with getattr(self, "lock"):
                        has_buffer = bool(getattr(self, "raw_buffer", None) or getattr(self, "snapshot_buffer", None) or getattr(self, "legacy_buffer", None))
                    if has_buffer:
                        self.flush()
                    now = time.time()
                    if now - last_idle_checkpoint >= idle_interval:
                        last_idle_checkpoint = now
                        wal_mb = _wal_mb(getattr(self, "db_path", None))
                        threshold = _env_float("RANKING_WRITER_WAL_TRUNCATE_MB", 128.0)
                        if wal_mb >= threshold:
                            _checkpoint(self, mode="TRUNCATE", reason="idle_wal_threshold")
                        elif _env_bool("RANKING_WRITER_IDLE_PASSIVE_CHECKPOINT", True):
                            _checkpoint(self, mode="PASSIVE", reason="idle")
                        if _env_bool("RANKING_WRITER_IDLE_GC", True):
                            gc.collect()
                    time.sleep(float(getattr(self, "flush_interval_sec", 1.0) or 1.0))
                except Exception:
                    logger.exception("[RANKING WAL GUARD] loop error")
                    time.sleep(1.0)

        _open_connection_patched._ranking_wal_guard_v1 = True  # type: ignore[attr-defined]
        _open_connection_patched._original = orig_open  # type: ignore[attr-defined]
        _flush_patched._ranking_wal_guard_v1 = True  # type: ignore[attr-defined]
        _flush_patched._original = orig_flush  # type: ignore[attr-defined]
        _loop_patched._ranking_wal_guard_v1 = True  # type: ignore[attr-defined]
        _loop_patched._original = orig_loop  # type: ignore[attr-defined]

        cls._open_connection = _open_connection_patched
        cls.flush = _flush_patched
        cls._loop = _loop_patched
        cls._ranking_wal_guard_v1 = True
        _INSTALLED = True
        logger.warning(
            "[RANKING WAL GUARD] installed v1 wal_truncate_mb=%.1f cache_kb=%s idle_checkpoint_sec=%.1f",
            _env_float("RANKING_WRITER_WAL_TRUNCATE_MB", 128.0),
            _env_int("RANKING_SQLITE_CACHE_KB", -8192),
            _env_float("RANKING_WRITER_IDLE_CHECKPOINT_SEC", 60.0),
        )
        return True
    except Exception:
        logger.exception("[RANKING WAL GUARD] apply failed")
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLING
    if _apply():
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        return
                    time.sleep(0.25)
                logger.warning("[RANKING WAL GUARD] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_loop, name="ranking-wal-guard-install", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[RANKING WAL GUARD] auto install failed")


__all__ = ["install"]
