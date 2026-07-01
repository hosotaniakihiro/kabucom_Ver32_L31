# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-LOCK-RETRYABLE"
_INSTALLED = False

LOCK_MARKERS = (
    "entry_controller_lock_timeout",
    "lock_timeout",
    "pipeline_busy",
    "already_running",
    "queued_async",
    "pending_moved_without_order",
)

NO_RETRY_MARKERS = (
    "snapshot_no_ai_approved_candidates",
    "no_tradable_rows_after_filters",
    "market_closed",
    "risk_guard_ng",
    "ai_health_ng",
    "index_shock",
    "invalid_pipeline_source",
)


def _reason_chain(mod: Any, result: Any) -> str:
    try:
        fn = getattr(mod, "_skip_reason", None)
        if callable(fn):
            return str(fn(result) or "")
    except Exception:
        pass
    reasons: list[str] = []
    try:
        cur = result
        for _ in range(16):
            if not isinstance(cur, dict):
                break
            for k in ("skip_reason", "lock_wait_reason", "reason", "status"):
                v = cur.get(k)
                if v:
                    reasons.append(str(v))
            nxt = cur.get("result") or cur.get("pipeline_result")
            if not isinstance(nxt, dict):
                break
            cur = nxt
    except Exception:
        pass
    return "|".join(reasons)


def _pending_counts(mod: Any, result: Any) -> tuple[int, int]:
    try:
        fn = getattr(mod, "_pending_counts", None)
        if callable(fn):
            before, after = fn(result)
            return int(before or 0), int(after or 0)
    except Exception:
        pass
    return 0, 0


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import core.startup.summary_ai_async_entry_patch as sap
    except Exception:
        logger.exception("[SUMMARY AI LOCK RETRY] import failed")
        return False

    os.environ["SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK"] = "1"
    os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY"] = "1"
    os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX"] = os.getenv("SUMMARY_AI_LOCK_RETRY_MAX", "3")
    os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC"] = os.getenv("SUMMARY_AI_LOCK_RETRY_SLEEP", "0.8")
    os.environ["SUMMARY_AI_ASYNC_ENTRY_STALE_SEC"] = os.getenv("SUMMARY_AI_LOCK_RETRY_STALE", "35")

    old = getattr(sap, "_is_retryable_controller_busy", None)
    if getattr(old, "_summary_ai_lock_retry_v1", False):
        _INSTALLED = True
        return True

    def _retryable(result: Any) -> bool:
        try:
            text = _reason_chain(sap, result).lower()
            if any(x in text for x in LOCK_MARKERS):
                return True
            if any(x in text for x in NO_RETRY_MARKERS):
                return False
            try:
                unwrap = getattr(sap, "_unwrap_result", None)
                unwrapped = unwrap(result) if callable(unwrap) else result
            except Exception:
                unwrapped = result
            if isinstance(unwrapped, dict) and bool(unwrapped.get("retryable")):
                return True
            before, after = _pending_counts(sap, result)
            if isinstance(result, dict) and not bool(result.get("executed")) and (before > 0 or after > 0):
                return True
            return False
        except Exception:
            return False

    _retryable._summary_ai_lock_retry_v1 = True  # type: ignore[attr-defined]
    _retryable._original = old  # type: ignore[attr-defined]
    sap._is_retryable_controller_busy = _retryable
    _INSTALLED = True
    logger.warning(
        "[SUMMARY AI LOCK RETRY] installed=True retry_max=%s sleep=%s stale=%s reentrant=%s version=%s",
        os.getenv("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX"),
        os.getenv("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC"),
        os.getenv("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC"),
        os.getenv("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK"),
        VERSION,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI LOCK RETRY] auto install failed")


__all__ = ["VERSION", "install"]
