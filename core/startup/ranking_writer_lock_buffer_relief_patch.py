# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/ranking_writer_lock_buffer_relief_patch.py
# Version: V3-RANKING-WRITER-DROP-LOCKED-RETRY-BUFFERS
# ------------------------------------------------------------
# Purpose:
#   NAS SQLite ranking DB can stay locked while summary enrichment reads the
#   large ranking_snapshot table.  When a flush fails, returning raw/snapshot
#   rows to the front of the buffer causes repeated lock loops.  For live
#   trading freshness, in-memory ranking/runtime symbols are more important than
#   preserving every failed DB write.
#
#   This patch:
#     - disables legacy buffering by default
#     - removes legacy before flush so V6 raw/snapshot path is used
#     - drops raw/snapshot retry buffers by default after locked flush failure
#     - keeps only if env explicitly asks to keep them
# ============================================================
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-RANKING-WRITER-DROP-LOCKED-RETRY-BUFFERS"
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


def _set_default_or_cap(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = value
            return
        # If caller inherited older large defaults, cap them down for live lock relief.
        if int(float(str(cur).replace(",", ""))) > int(float(value)):
            os.environ[name] = value
    except Exception:
        os.environ[name] = value


def _trim_buffers(writer, *, reason: str) -> dict[str, int]:
    # By default drop failed retry buffers.  Set RANKING_WRITER_KEEP_LOCKED_RETRY_ROWS=1
    # to keep a small tail for forensic persistence.
    keep_locked = _env_bool("RANKING_WRITER_KEEP_LOCKED_RETRY_ROWS", False)
    raw_limit = _env_int("RANKING_WRITER_RETRY_RAW_MAX_ROWS", 300 if keep_locked else 0)
    snapshot_limit = _env_int("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS", 300 if keep_locked else 0)
    keep_legacy = _env_bool("RANKING_WRITER_RETRY_KEEP_LEGACY", False)
    legacy_limit = _env_int("RANKING_WRITER_RETRY_LEGACY_MAX_ROWS", 0 if not keep_legacy else 300)

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
            "[RANKING WRITER RELIEF] trimmed buffers reason=%s before=%s after=%s limits={raw:%s,snapshot:%s,legacy:%s} keep_locked=%s",
            reason,
            before,
            after,
            raw_limit,
            snapshot_limit,
            legacy_limit,
            keep_locked,
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

        os.environ.setdefault("RANKING_WRITER_KEEP_LOCKED_RETRY_ROWS", "0")
        _set_default_or_cap("RANKING_WRITER_RETRY_RAW_MAX_ROWS", "0")
        _set_default_or_cap("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS", "0")
        os.environ.setdefault("RANKING_WRITER_RETRY_KEEP_LEGACY", "0")
        _set_default_or_cap("RANKING_WRITER_RETRY_LEGACY_MAX_ROWS", "0")
        os.environ.setdefault("RANKING_WRITER_DISABLE_LEGACY_ON_LOCK_RELIEF", "1")
        os.environ.setdefault("RANKING_WRITER_BUFFER_SIZE", "1000")
        os.environ.setdefault("RANKING_WRITER_FLUSH_ON_THRESHOLD", "0")
        os.environ.setdefault("RANKING_DB_WRITER_BUSY_TIMEOUT_MS", "30000")
        os.environ.setdefault("RANKING_WRITER_LOCK_RETRY_MAX", "1")

        old_add = getattr(cls, "add_ranking_rows", None)
        if callable(old_add) and not getattr(old_add, "_ranking_writer_relief_v3", False):
            @wraps(old_add)
            def _patched_add(self, *args, **kwargs):
                if _env_bool("RANKING_WRITER_DISABLE_LEGACY_ON_LOCK_RELIEF", True):
                    kwargs["save_legacy"] = False
                    try:
                        self.enable_legacy_save = False
                    except Exception:
                        pass
                ret = old_add(self, *args, **kwargs)
                # Do not keep old failed buffers after new rows arrive.
                _trim_buffers(self, reason="after_add")
                return ret

            _patched_add._ranking_writer_relief_v3 = True  # type: ignore[attr-defined]
            _patched_add._ranking_writer_relief_v2 = True  # type: ignore[attr-defined]
            _patched_add._original = old_add  # type: ignore[attr-defined]
            cls.add_ranking_rows = _patched_add

        old_flush = getattr(cls, "flush", None)
        if callable(old_flush) and not getattr(old_flush, "_ranking_writer_relief_v3", False):
            @wraps(old_flush)
            def _patched_flush(self, *args, **kwargs):
                dropped_legacy = _drop_legacy_before_flush(self)
                ok = bool(old_flush(self, *args, **kwargs))
                if not ok:
                    _trim_buffers(self, reason="flush_failed_drop_locked")
                else:
                    # also remove any old retained retry rows after success
                    _trim_buffers(self, reason="flush_ok_cleanup")
                if dropped_legacy:
                    logger.warning(
                        "[RANKING WRITER RELIEF] primary flush result after legacy split ok=%s dropped_legacy=%d",
                        ok,
                        dropped_legacy,
                    )
                return ok

            _patched_flush._ranking_writer_relief_v3 = True  # type: ignore[attr-defined]
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
            "[RANKING WRITER RELIEF] installed version=%s raw_limit=%s snapshot_limit=%s keep_legacy=%s keep_locked=%s buffer_size_env=%s",
            VERSION,
            os.environ.get("RANKING_WRITER_RETRY_RAW_MAX_ROWS"),
            os.environ.get("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS"),
            os.environ.get("RANKING_WRITER_RETRY_KEEP_LEGACY"),
            os.environ.get("RANKING_WRITER_KEEP_LOCKED_RETRY_ROWS"),
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
