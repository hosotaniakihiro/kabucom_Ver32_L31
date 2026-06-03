# ============================================================
# File   : core/startup/ranking_wal_aggressive_truncate_patch.py
# Version: V1-RANKING-WAL-AGGRESSIVE-TRUNCATE-DEFAULTS
# ------------------------------------------------------------
# Purpose:
#   rankingYYYYMMDD.db-wal was observed around 99MB while the existing
#   guard only ran PASSIVE checkpoint because the default TRUNCATE threshold
#   was 128MB.  Set safer defaults so idle/after-flush checks choose
#   TRUNCATE earlier.
# ============================================================
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    # Existing ranking_wal_checkpoint_memory_guard_patch reads these envs
    # dynamically, so applying this after that patch is still effective.
    os.environ.setdefault("RANKING_WRITER_WAL_TRUNCATE_MB", "64")
    os.environ.setdefault("RANKING_WRITER_IDLE_CHECKPOINT_SEC", "20")
    os.environ.setdefault("RANKING_WRITER_PASSIVE_CHECKPOINT_AFTER_FLUSH", "1")
    os.environ.setdefault("RANKING_WRITER_IDLE_PASSIVE_CHECKPOINT", "1")
    os.environ.setdefault("RANKING_WRITER_GC_AFTER_FLUSH", "1")
    os.environ.setdefault("RANKING_WRITER_IDLE_GC", "1")
    os.environ.setdefault("RANKING_SQLITE_CACHE_KB", "-8192")
    os.environ.setdefault("RANKING_WRITER_WAL_AUTOCHECKPOINT", "100")
    os.environ.setdefault("RANKING_SQLITE_TEMP_STORE", "FILE")

    try:
        import core.startup.ranking_wal_checkpoint_memory_guard_patch as guard
        fn = getattr(guard, "install", None)
        if callable(fn):
            fn()
    except Exception:
        logger.debug("[RANKING WAL AGGRESSIVE] guard re-install/check failed", exc_info=True)

    _INSTALLED = True
    logger.warning(
        "[RANKING WAL AGGRESSIVE] installed truncate_mb=%s idle_checkpoint_sec=%s wal_autocheckpoint=%s",
        os.getenv("RANKING_WRITER_WAL_TRUNCATE_MB"),
        os.getenv("RANKING_WRITER_IDLE_CHECKPOINT_SEC"),
        os.getenv("RANKING_WRITER_WAL_AUTOCHECKPOINT"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[RANKING WAL AGGRESSIVE] auto install failed")


__all__ = ["install"]
