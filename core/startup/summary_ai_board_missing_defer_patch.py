# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-BOARD-MISSING-DEFER-FULL-ROTATION-PENDING-LIQ-FALLBACK"
_INSTALLED = False
_DEFERRED: dict[tuple[str, str], float] = {}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _is_board_missing_result(v: Any) -> bool:
    try:
        text = str(v)
        return "STRICT_BOARD_MISSING" in text or "板が取れない" in text or "board missing" in text.lower()
    except Exception:
        return False


def _mark_deferred(symbol: str, side: str) -> int:
    now = time.time()
    ttl = max(1.0, _env_float("SUMMARY_AI_BOARD_MISSING_DEFER_TTL_SEC", 12.0))
    key = (_norm_symbol(symbol), str(side or "").upper())
    _DEFERRED[key] = now + ttl
    return int(sum(1 for exp in _DEFERRED.values() if exp > now))


def _apply_full_rotation_retry() -> bool:
    """Wait long enough to observe both A/B PUSH batches before hard-blocking."""
    try:
        retry_sec = max(10.0, _env_float("SUMMARY_AI_BOARD_FULL_ROTATION_RETRY_SEC", 10.5))
        interval_sec = max(0.1, min(_env_float("SUMMARY_AI_BOARD_FULL_ROTATION_RETRY_INTERVAL_SEC", 0.2), 1.0))
        os.environ["ENTRY_ORDER_BOARD_RETRY_SEC"] = str(retry_sec)
        os.environ["ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"] = str(interval_sec)
        os.environ["SUMMARY_AI_BOARD_RETRY_REASON"] = "push_rotation_full_cycle_wait"
        os.environ["SUMMARY_AI_BOARD_MISSING_DEFER_TTL_SEC"] = str(max(12.0, retry_sec + 2.0))
        os.environ["ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY"] = "1"
        os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "1"
        os.environ["ENTRY_LIMIT_ALLOW_WITHOUT_BOARD"] = "0"
        os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = "0"
        try:
            from trading.handlers import entry_order_builder as eob
            old_retry = getattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", None)
            old_interval = getattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", None)
            setattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", retry_sec)
            setattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", interval_sec)
            try:
                setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", True)
            except Exception:
                pass
            logger.warning(
                "[SUMMARY AI BOARD MISSING DEFER] full rotation retry applied retry_sec %s->%s interval %s->%s hard_block=True version=%s",
                old_retry,
                retry_sec,
                old_interval,
                interval_sec,
                VERSION,
            )
        except Exception:
            logger.debug("[SUMMARY AI BOARD MISSING DEFER] eob full rotation retry skipped", exc_info=True)
        return True
    except Exception:
        logger.exception("[SUMMARY AI BOARD MISSING DEFER] full rotation retry apply failed version=%s", VERSION)
        return False


def _install_pending_liq_patch() -> bool:
    try:
        from core.startup.entry_handler_recent_liq_pending_fallback_patch import install as _install
        ok = bool(_install())
        logger.warning("[SUMMARY AI BOARD MISSING DEFER] chained pending_liq_fallback ok=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI BOARD MISSING DEFER] chained pending_liq_fallback failed version=%s", VERSION)
        return False


def _install_async_snapshot_patch() -> bool:
    try:
        import core.startup.summary_ai_async_entry_patch as ap
        cur = getattr(ap, "_summary_ai_direct_snapshot_execute", None)
        if not callable(cur):
            return False
        if getattr(cur, "_board_missing_defer_v3", False):
            return True

        @wraps(cur)
        def wrapped(entries, *args, **kwargs):
            result = cur(entries, *args, **kwargs)
            try:
                if isinstance(result, dict) and not bool(result.get("executed")):
                    order_results = result.get("result") or []
                    if isinstance(order_results, list):
                        deferred = 0
                        for r in order_results:
                            if isinstance(r, dict) and not bool(r.get("ok")) and _is_board_missing_result(r):
                                deferred = _mark_deferred(r.get("symbol"), r.get("side"))
                        if deferred:
                            result = dict(result)
                            result["retryable"] = True
                            result["skip_reason"] = "board_missing_deferred"
                            result["board_missing_deferred"] = deferred
                            logger.warning("[SUMMARY AI BOARD MISSING DEFER] snapshot defer count=%s version=%s", deferred, VERSION)
            except Exception:
                logger.debug("[SUMMARY AI BOARD MISSING DEFER] snapshot post process failed", exc_info=True)
            return result

        wrapped._board_missing_defer_v1 = True  # type: ignore[attr-defined]
        wrapped._board_missing_defer_v2 = True  # type: ignore[attr-defined]
        wrapped._board_missing_defer_v3 = True  # type: ignore[attr-defined]
        wrapped._original = cur  # type: ignore[attr-defined]
        ap._summary_ai_direct_snapshot_execute = wrapped
        logger.warning("[SUMMARY AI BOARD MISSING DEFER] async snapshot patched version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI BOARD MISSING DEFER] async snapshot patch failed version=%s", VERSION)
        return False


def _install_fast_builder_log_patch() -> bool:
    try:
        import core.startup.summary_ai_fast_order_builder_patch as fb
        setattr(fb, "SUMMARY_AI_BOARD_MISSING_DEFER_ENABLED", True)
        logger.warning("[SUMMARY AI BOARD MISSING DEFER] fast builder marker set version=%s", VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI BOARD MISSING DEFER] fast builder marker skipped", exc_info=True)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        _apply_full_rotation_retry()
        _install_pending_liq_patch()
        return True
    try:
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", "1")
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", "3")
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", "35")
        ok0 = _apply_full_rotation_retry()
        ok1 = _install_async_snapshot_patch()
        ok2 = _install_fast_builder_log_patch()
        ok3 = _install_pending_liq_patch()
        _INSTALLED = bool(ok0 or ok1 or ok2 or ok3)
        logger.warning(
            "[SUMMARY AI BOARD MISSING DEFER] installed ok=%s full_rotation=%s async=%s builder=%s pending_liq=%s retry_sec=%s ttl=%s version=%s",
            _INSTALLED,
            ok0,
            ok1,
            ok2,
            ok3,
            os.getenv("ENTRY_ORDER_BOARD_RETRY_SEC"),
            os.getenv("SUMMARY_AI_BOARD_MISSING_DEFER_TTL_SEC"),
            VERSION,
        )
        return _INSTALLED
    except Exception:
        logger.exception("[SUMMARY AI BOARD MISSING DEFER] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BOARD MISSING DEFER] auto install failed")


__all__ = ["install", "VERSION"]
