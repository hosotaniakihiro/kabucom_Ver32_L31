# -*- coding: utf-8 -*-
"""
Compatibility wrapper for ranking writer lock relief.

Older startup code calls this module as `ranking_legacy_inline_flush_patch`.
On 2026-06-29 logs, legacy rows were the lock amplifier: legacy_buffer grew to
40k+ rows while raw/snapshot flush repeatedly failed at snapshot DELETE with
`database is locked`.  For live trading, ranking_raw and ranking_snapshot are the
primary tables; legacy compatibility tables are optional.

This wrapper now installs ranking_writer_lock_buffer_relief_patch, which disables
legacy buffering by default and trims failed retry buffers so ranking DB writes
recover instead of growing forever.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
VERSION = "V2-LEGACY-INLINE-TO-BUFFER-RELIEF"
_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if str(os.getenv("DISABLE_RANKING_LEGACY_INLINE_FLUSH_PATCH", "")).strip() == "1":
        logger.warning("[RANKING LEGACY INLINE FLUSH] disabled by env")
        return False
    try:
        os.environ.setdefault("RANKING_WRITER_DISABLE_LEGACY_ON_LOCK_RELIEF", "1")
        os.environ.setdefault("RANKING_WRITER_RETRY_KEEP_LEGACY", "0")
        os.environ.setdefault("RANKING_WRITER_RETRY_LEGACY_MAX_ROWS", "0")
        os.environ.setdefault("RANKING_WRITER_RETRY_RAW_MAX_ROWS", "3000")
        os.environ.setdefault("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS", "3000")
        os.environ.setdefault("RANKING_WRITER_BUFFER_SIZE", "1000")
        os.environ.setdefault("RANKING_WRITER_FLUSH_ON_THRESHOLD", "0")
        os.environ.setdefault("RANKING_DB_WRITER_BUSY_TIMEOUT_MS", "30000")

        from . import ranking_writer_lock_buffer_relief_patch
        ok = bool(ranking_writer_lock_buffer_relief_patch.install())
        _INSTALLED = ok
        logger.warning(
            "[RANKING LEGACY INLINE FLUSH] installed wrapper version=%s relief_ok=%s legacy_disabled=%s raw_limit=%s snapshot_limit=%s",
            VERSION,
            ok,
            os.environ.get("RANKING_WRITER_DISABLE_LEGACY_ON_LOCK_RELIEF"),
            os.environ.get("RANKING_WRITER_RETRY_RAW_MAX_ROWS"),
            os.environ.get("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS"),
        )
        return ok
    except Exception:
        logger.exception("[RANKING LEGACY INLINE FLUSH] relief wrapper install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING LEGACY INLINE FLUSH] auto install failed")

__all__ = ["VERSION", "install"]
