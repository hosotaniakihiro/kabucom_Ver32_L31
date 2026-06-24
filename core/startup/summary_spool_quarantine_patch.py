# -*- coding: utf-8 -*-
"""
Quarantine useless summary spool files so they do not get retried forever.

Observed issue:
  SUMMARY SAVE SPOOL repeatedly retries old files with rows=0, for example
  summary_spool_20260610_push_1m_...jsonl.gz rows=0, which keeps
  failed_files=2 and adds avoidable work to every summary tick.

This patch wraps trading.summary.persistence.summary_save_spool.flush_summary_spool.
It moves unreadable, empty, and too-old spool files away from the
summary_spool_*.jsonl.gz glob before delegating to the original flusher.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-SUMMARY-SPOOL-EMPTY-OLD-QUARANTINE"
_INSTALLED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, str(default))).strip()))
    except Exception:
        return int(default)


def _safe_rename_with_suffix(path: Path, suffix: str) -> Path | None:
    try:
        dst = path.with_suffix(path.suffix + suffix)
        if dst.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            dst = path.with_name(f"{path.name}.{stamp}{suffix}")
        path.rename(dst)
        return dst
    except Exception:
        logger.exception("[SUMMARY SPOOL QUARANTINE] rename failed path=%s suffix=%s", path, suffix)
        return None


def _spool_ymd_from_name_or_meta(path: Path, meta: dict[str, Any]) -> str:
    try:
        ymd = str(meta.get("date_yyyymmdd") or "").strip()
        if len(ymd) == 8 and ymd.isdigit():
            return ymd
    except Exception:
        pass
    try:
        # summary_spool_20260610_push_1m_...
        parts = path.name.split("_")
        for part in parts:
            if len(part) == 8 and part.isdigit():
                return part
    except Exception:
        pass
    return ""


def _is_old_ymd(ymd: str, max_age_days: int) -> bool:
    if max_age_days < 0:
        return False
    try:
        d = dt.datetime.strptime(str(ymd), "%Y%m%d").date()
        return (dt.date.today() - d).days > int(max_age_days)
    except Exception:
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_SPOOL_QUARANTINE_ENABLED", True):
        logger.warning("[SUMMARY SPOOL QUARANTINE] disabled by env")
        return False

    try:
        import trading.summary.persistence.summary_save_spool as spool

        old_flush = getattr(spool, "flush_summary_spool", None)
        read_spool = getattr(spool, "_read_spool", None)
        spool_dir = getattr(spool, "_spool_dir", None)
        bad_errors = getattr(spool, "_BAD_GZIP_ERRORS", (Exception,))
        if not callable(old_flush) or not callable(read_spool) or not callable(spool_dir):
            logger.warning("[SUMMARY SPOOL QUARANTINE] install skipped missing spool funcs")
            return False
        if getattr(old_flush, "_summary_spool_quarantine_patch", False):
            _INSTALLED = True
            return True

        max_age_days = _env_int("SUMMARY_SPOOL_QUARANTINE_MAX_AGE_DAYS", 2)
        quarantine_empty = _env_bool("SUMMARY_SPOOL_QUARANTINE_EMPTY", True)
        quarantine_old = _env_bool("SUMMARY_SPOOL_QUARANTINE_OLD", True)

        def _patched_flush_summary_spool(*, max_files: int = 50) -> dict:
            quarantined_empty = 0
            quarantined_old = 0
            quarantined_bad = 0
            try:
                d = spool_dir()
                files = sorted(d.glob("summary_spool_*.jsonl.gz"), key=lambda p: p.stat().st_mtime)[:max_files]
                for path in files:
                    try:
                        meta, df = read_spool(path)
                    except bad_errors as e:  # type: ignore[misc]
                        dst = _safe_rename_with_suffix(path, ".bad")
                        quarantined_bad += 1 if dst is not None else 0
                        logger.error("[SUMMARY SPOOL QUARANTINE] bad spool quarantined path=%s dst=%s err=%r", path, dst, e)
                        continue
                    except Exception:
                        # Unknown read error: leave for original flusher to log/handle.
                        continue

                    ymd = _spool_ymd_from_name_or_meta(path, meta if isinstance(meta, dict) else {})
                    rows = len(df) if hasattr(df, "__len__") else 0
                    if quarantine_empty and int(rows or 0) <= 0:
                        dst = _safe_rename_with_suffix(path, ".empty")
                        quarantined_empty += 1 if dst is not None else 0
                        logger.warning(
                            "[SUMMARY SPOOL QUARANTINE] empty spool quarantined path=%s dst=%s ymd=%s rows=%s",
                            path,
                            dst,
                            ymd,
                            rows,
                        )
                        continue
                    if quarantine_old and ymd and _is_old_ymd(ymd, max_age_days):
                        dst = _safe_rename_with_suffix(path, ".old")
                        quarantined_old += 1 if dst is not None else 0
                        logger.warning(
                            "[SUMMARY SPOOL QUARANTINE] old spool quarantined path=%s dst=%s ymd=%s max_age_days=%s rows=%s",
                            path,
                            dst,
                            ymd,
                            max_age_days,
                            rows,
                        )
                        continue
            except Exception:
                logger.exception("[SUMMARY SPOOL QUARANTINE] pre-scan failed")

            result = old_flush(max_files=max_files)
            try:
                if isinstance(result, dict):
                    result["quarantined_empty"] = int(quarantined_empty)
                    result["quarantined_old"] = int(quarantined_old)
                    result["quarantined_bad"] = int(quarantined_bad)
            except Exception:
                pass
            return result

        _patched_flush_summary_spool._summary_spool_quarantine_patch = True  # type: ignore[attr-defined]
        _patched_flush_summary_spool._original = old_flush  # type: ignore[attr-defined]
        spool.flush_summary_spool = _patched_flush_summary_spool
        _INSTALLED = True
        logger.warning(
            "[SUMMARY SPOOL QUARANTINE] installed version=%s empty=%s old=%s max_age_days=%s",
            VERSION,
            quarantine_empty,
            quarantine_old,
            max_age_days,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY SPOOL QUARANTINE] install failed")
        return False


__all__ = ["VERSION", "install"]
