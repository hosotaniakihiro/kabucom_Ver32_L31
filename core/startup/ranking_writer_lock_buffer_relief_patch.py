# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/ranking_writer_lock_buffer_relief_patch.py
# Version: V2-RANKING-WRITER-LOCK-BUFFER-RELIEF-SPLIT-LEGACY
# ------------------------------------------------------------
# Purpose:
#   Ranking DB writer repeatedly failed at snapshot DELETE with
#   sqlite3.OperationalError: database is locked.  A key amplifier was that
#   legacy_buffer was mixed with raw/snapshot rows.  The V6 lock patch delegates
#   mixed legacy flushes back to the original writer, so the original DELETE path
#   was still used and buffers grew to tens of thousands of rows.
#
#   This patch keeps ranking runtime fresh by:
#     - disabling legacy ranking-table buffering by default in live fast mode
#     - removing legacy_buffer before flush so V6 raw/snapshot path is used
#     - trimming raw/snapshot retry buffers after a failed flush
#     - dropping legacy retry buffers after a failed flush
# ============================================================
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V2-RANKING-WRITER-LOCK-BUFFER-RELIEF-SPLIT-LEGACY"
_INSTALLED = False


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _trim_list(values: Any, limit: int) -> list:
    try:
        if limit <= 0:
            return []
        xs = list(values or [])
        if len(xs) <= limit:
            return xs
        return xs[-limit:]
    except Exception:
        return []


def _trim_buffers(writer, *, reason: str) -> dict[str, int]:
    raw_limit = _env_int("RANKING_WRITER_RETRY_RAW_MAX_ROWS", 3000)
    snapshot_limit = _env_int("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS", 3000)
    keep_legacy = _env_bool("RANKING_WRITER_RETRY_KEEP_LEGACY", False)
    legacy_limit = _env_int("RANKING_WRITER_RETRY_LEGACY_MAX_ROWS", 0 if not keep_legacy else 1000)

    with writer.lock:
        before = {
            "raw": len(getattr(writer, "raw_buffer", []) or []),
            "snapshot": len(getattr(writer, "snapshot_buffer", []) or []),
            "legacy": len(getattr(writer, "legacy_buffer", []) or []),
        }
        writer.raw_buffer = _trim_list(getattr(writer, "raw_buffer", []), raw_limit)
        writer.snapshot_buffer = _trim_list(getattr(writer, "snapshot_buffer", []), snapshot_limit)
        writer.legacy_buffer = _trim_list(getattr(writer, "legacy_buffer", []), legacy_limit)
        after = {
            "raw": len(getattr(writer, "raw_buffer", []) or []),
            "snapshot": len(getattr(writer, "snapshot_buffer", []) or []),
            "legacy": len(getattr(writer, "legacy_buffer", []) or []),
        }
        try:
            writer._mark_runtime()
        except Exception:
            pass
    if before != after:
        logger.warning(
            "[RANKING WRITER RELIEF] trimmed buffers reason=%s before=%s after=%s limits={raw:%s,snapshot:%s,legacy:%s}",
            reason,
            before,
            after,
            raw_limit,
            snapshot_limit,
            legacy_limit,
        )
    return after


def _drop_legacy_before_flush(writer) -> int:
    if _env_bool("RANKING_WRITER_RETRY_KEEP_LEGACY", False):
        return 0
    with writer.lock:
        legacy_n = len(getattr(writer, "legacy_buffer", []) or [])
        if legacy_n:
            writer.legacy_buffer = []
            try:
                writer.enable_legacy_save = False
            except Exception:
                pass
            try:
                writer._mark_runtime()
            except Exception:
                pass
    if legacy_n:
        logger.warning(
            "[RANKING WRITER RELIEF] dropped legacy before primary flush legacy=%d reason=avoid_original_writer_delete_lock",
            legacy_n,
        )
    return legacy_n


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.ranking.ranking_db_writer as mod

        cls = getattr(mod, "RankingDBWriter", None)
        if cls is None:
            logger.warning("[RANKING WRITER RELIEF] RankingDBWriter class missing")
            return False

        os.environ.setdefault("RANKING_WRITER_RETRY_RAW_MAX_ROWS", "3000")
        os.environ.setdefault("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS", "3000")
        os.environ.setdefault("RANKING_WRITER_RETRY_KEEP_LEGACY", "0")
        os.environ.setdefault("RANKING_WRITER_RETRY_LEGACY_MAX_ROWS", "0")
        os.environ.setdefault("RANKING_WRITER_DISABLE_LEGACY_ON_LOCK_RELIEF", "1")
        os.environ.setdefault("RANKING_WRITER_BUFFER_SIZE", "1000")
        os.environ.setdefault("RANKING_WRITER_FLUSH_ON_THRESHOLD", "0")
        os.environ.setdefault("RANKING_DB_WRITER_BUSY_TIMEOUT_MS", "30000")

        old_add = getattr(cls, "add_ranking_rows", None)
        if callable(old_add) and not getattr(old_add, "_ranking_writer_relief_v2", False):
            @wraps(old_add)
            def _patched_add(self, *args, **kwargs):
                if _env_bool("RANKING_WRITER_DISABLE_LEGACY_ON_LOCK_RELIEF", True):
                    kwargs["save_legacy"] = False
                    try:
                        self.enable_legacy_save = False
                    except Exception:
                        pass
                ret = old_add(self, *args, **kwargs)
                _trim_buffers(self, reason="after_add")
                return ret

            _patched_add._ranking_writer_relief_v2 = True  # type: ignore[attr-defined]
            _patched_add._original = old_add  # type: ignore[attr-defined]
            cls.add_ranking_rows = _patched_add

        old_flush = getattr(cls, "flush", None)
        if callable(old_flush) and not getattr(old_flush, "_ranking_writer_relief_v2", False):
            @wraps(old_flush)
            def _patched_flush(self, *args, **kwargs):
                dropped_legacy = _drop_legacy_before_flush(self)
                ok = bool(old_flush(self, *args, **kwargs))
                if not ok:
                    _trim_buffers(self, reason="flush_failed")
                else:
                    _trim_buffers(self, reason="flush_ok_cleanup")
                if dropped_legacy:
                    logger.warning(
                        "[RANKING WRITER RELIEF] primary flush result after legacy split ok=%s dropped_legacy=%d",
                        ok,
                        dropped_legacy,
                    )
                return ok

            _patched_flush._ranking_writer_relief_v2 = True  # type: ignore[attr-defined]
            _patched_flush._original = old_flush  # type: ignore[attr-defined]
            cls.flush = _patched_flush

        try:
            w = getattr(mod, "ranking_writer", None)
            if w is not None:
                w.enable_legacy_save = False
                w.buffer_size = max(int(getattr(w, "buffer_size", 1) or 1), _env_int("RANKING_WRITER_BUFFER_SIZE", 1000))
                _drop_legacy_before_flush(w)
                _trim_buffers(w, reason="install")
        except Exception:
            logger.debug("[RANKING WRITER RELIEF] singleton tune skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[RANKING WRITER RELIEF] installed version=%s raw_limit=%s snapshot_limit=%s keep_legacy=%s buffer_size_env=%s",
            VERSION,
            os.environ.get("RANKING_WRITER_RETRY_RAW_MAX_ROWS"),
            os.environ.get("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS"),
            os.environ.get("RANKING_WRITER_RETRY_KEEP_LEGACY"),
            os.environ.get("RANKING_WRITER_BUFFER_SIZE"),
        )
        return True
    except Exception:
        logger.exception("[RANKING WRITER RELIEF] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING WRITER RELIEF] auto install failed")

__all__ = ["VERSION", "install"]
