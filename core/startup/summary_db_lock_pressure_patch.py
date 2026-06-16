# -*- coding: utf-8 -*-
"""
Runtime hotfix for summary DB lock pressure.

Symptoms handled:
  - sqlite3.OperationalError: database is locked at BEGIN IMMEDIATE
  - periodic summary saves writing thousands of historical rows with save_reason=""
  - Yahoo complement futures piling up while summary DB writer is blocked

Policy:
  - bootstrap/recovery/backfill saves keep full-history behavior
  - realtime/periodic/empty-reason saves store only the latest row per symbol
  - realtime saves skip quickly when the DB is already busy instead of blocking for minutes
  - avoid per-upsert WAL checkpoint in the hot path
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "REV1-SUMMARY-DB-LOCK-PRESSURE-HOTFIX"
_INSTALLED = False
_ORIGINAL_BULK_UPSERT = None
_ORIGINAL_SHOULD_AUTO_LATEST_ONLY = None

_MAINTENANCE_KEYWORDS = (
    "bootstrap",
    "rebuild",
    "recovery",
    "recover",
    "backfill",
    "full",
    "repair",
    "migrate",
    "migration",
    "historical",
    "history",
    "catchup",
    "startup",
)

_REALTIME_KEYWORDS = (
    "periodic",
    "tick",
    "push",
    "yahoo",
    "display",
    "latest",
    "scheduled",
    "scheduler",
    "regular",
    "summary_upsert",
    "auto_realtime",
)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except Exception:
        return int(default)


def _reason_text(save_reason: Any) -> str:
    try:
        return str(save_reason or "").strip().lower()
    except Exception:
        return ""


def _is_maintenance_reason(save_reason: Any) -> bool:
    r = _reason_text(save_reason)
    return bool(r and any(k in r for k in _MAINTENANCE_KEYWORDS))


def _is_realtime_reason(save_reason: Any) -> bool:
    r = _reason_text(save_reason)
    if not r:
        return True
    if _is_maintenance_reason(r):
        return False
    return any(k in r for k in _REALTIME_KEYWORDS)


def _row_count(df: Any) -> int:
    try:
        return int(len(df))
    except Exception:
        return -1


def _patched_should_auto_latest_only(save_reason: str) -> bool:
    if _is_maintenance_reason(save_reason):
        return False
    if _is_realtime_reason(save_reason):
        return True
    try:
        if callable(_ORIGINAL_SHOULD_AUTO_LATEST_ONLY):
            return bool(_ORIGINAL_SHOULD_AUTO_LATEST_ONLY(save_reason))
    except Exception:
        logger.debug("[SUMMARY LOCK PRESSURE] original latest_only detector failed", exc_info=True)
    return False


def _patched_bulk_upsert_summary(
    df,
    interval: int,
    lock_timeout_sec: float = None,
    skip_if_busy: bool = False,
    latest_only: bool = False,
    save_reason: str = "",
) -> int:
    if not callable(_ORIGINAL_BULK_UPSERT):
        return 0

    try:
        reason = _reason_text(save_reason)
        rows = _row_count(df)
        threshold = _env_int("SUMMARY_REALTIME_LATEST_ONLY_ROW_THRESHOLD", 500)
        force_latest_for_realtime = _env_bool("SUMMARY_REALTIME_FORCE_LATEST_ONLY", True)
        quick_skip = _env_bool("SUMMARY_REALTIME_SKIP_IF_BUSY", True)
        timeout_cap = _env_float("SUMMARY_REALTIME_LOCK_TIMEOUT_SEC", 2.0)

        maintenance = _is_maintenance_reason(reason)
        realtime_like = _is_realtime_reason(reason)
        should_shrink = (not maintenance) and force_latest_for_realtime and (realtime_like or rows >= threshold)

        if should_shrink:
            latest_only = True
            if quick_skip:
                skip_if_busy = True
            if not reason:
                save_reason = "auto_realtime_empty_reason"
            try:
                if lock_timeout_sec is None:
                    lock_timeout_sec = timeout_cap
                else:
                    lock_timeout_sec = min(float(lock_timeout_sec), timeout_cap)
            except Exception:
                lock_timeout_sec = timeout_cap

            if rows >= threshold or not reason:
                logger.warning(
                    "[SUMMARY LOCK PRESSURE] realtime save compressed interval=%s rows=%s latest_only=%s skip_if_busy=%s timeout=%.2fs reason=%s",
                    interval,
                    rows,
                    latest_only,
                    skip_if_busy,
                    float(lock_timeout_sec or timeout_cap),
                    save_reason,
                )
    except Exception:
        logger.debug("[SUMMARY LOCK PRESSURE] wrapper precheck failed", exc_info=True)

    return _ORIGINAL_BULK_UPSERT(
        df,
        interval=interval,
        lock_timeout_sec=lock_timeout_sec if lock_timeout_sec is not None else 2.0,
        skip_if_busy=skip_if_busy,
        latest_only=latest_only,
        save_reason=save_reason,
    )


def install() -> bool:
    global _INSTALLED, _ORIGINAL_BULK_UPSERT, _ORIGINAL_SHOULD_AUTO_LATEST_ONLY
    if _INSTALLED:
        return True

    try:
        # Defaults must be set before sqlite/upsert modules are imported where possible.
        os.environ.setdefault("SUMMARY_REALTIME_FORCE_LATEST_ONLY", "1")
        os.environ.setdefault("SUMMARY_REALTIME_SKIP_IF_BUSY", "1")
        os.environ.setdefault("SUMMARY_REALTIME_LOCK_TIMEOUT_SEC", "2.0")
        os.environ.setdefault("SUMMARY_REALTIME_LATEST_ONLY_ROW_THRESHOLD", "500")
        os.environ.setdefault("SUMMARY_UPSERT_WAL_CHECKPOINT", "0")
        os.environ.setdefault("SQLITE_BUSY_TIMEOUT_MS", "45000")
        os.environ.setdefault("SUMMARY_UPSERT_CHUNK_SIZE", "250")
        os.environ.setdefault("SUMMARY_UPSERT_RETRY", "6")
        os.environ.setdefault("SUMMARY_UPSERT_LOCK_BACKOFF_MAX_SEC", "1.2")

        from trading.summary.persistence import summary_saver_bulk as saver

        if getattr(saver.bulk_upsert_summary, "_summary_lock_pressure_patched", False):
            _INSTALLED = True
            return True

        _ORIGINAL_BULK_UPSERT = saver.bulk_upsert_summary
        _ORIGINAL_SHOULD_AUTO_LATEST_ONLY = getattr(saver, "_should_auto_latest_only", None)

        saver._should_auto_latest_only = _patched_should_auto_latest_only
        _patched_bulk_upsert_summary._summary_lock_pressure_patched = True  # type: ignore[attr-defined]
        saver.bulk_upsert_summary = _patched_bulk_upsert_summary
        saver.save_summary_bulk = _patched_bulk_upsert_summary
        saver.save_summary_df = _patched_bulk_upsert_summary

        # If upsert_executor was already imported, turn off hot-path checkpoint there as well.
        try:
            from trading.summary.persistence.core import upsert_executor
            upsert_executor._CHECKPOINT_AFTER_UPSERT = False
        except Exception:
            pass

        _INSTALLED = True
        logger.warning(
            "[SUMMARY LOCK PRESSURE] installed version=%s realtime_latest_only=%s skip_if_busy=%s timeout=%s threshold=%s wal_checkpoint=%s",
            VERSION,
            os.environ.get("SUMMARY_REALTIME_FORCE_LATEST_ONLY"),
            os.environ.get("SUMMARY_REALTIME_SKIP_IF_BUSY"),
            os.environ.get("SUMMARY_REALTIME_LOCK_TIMEOUT_SEC"),
            os.environ.get("SUMMARY_REALTIME_LATEST_ONLY_ROW_THRESHOLD"),
            os.environ.get("SUMMARY_UPSERT_WAL_CHECKPOINT"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY LOCK PRESSURE] install failed")
        return False


__all__ = ["VERSION", "install"]
