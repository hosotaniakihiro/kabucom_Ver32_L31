from __future__ import annotations

import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    return s if s in {"BUY", "SELL"} else "BUY"


def _is_sell_reject_or_credit_ng(symbol: str, side: str) -> Tuple[bool, Any]:
    symbol_s = _norm_symbol(symbol)
    side_s = _norm_side(side)
    if side_s != "SELL" or not symbol_s:
        return False, None

    try:
        from AI.sell_order_reject_cache import is_sell_rejected, get_sell_reject_reason
        if is_sell_rejected(symbol_s):
            return True, {"reason": "sell_reject_cache", "detail": str(get_sell_reject_reason(symbol_s))}
    except Exception:
        pass

    try:
        from AI.sell_credit_guard import can_sell_symbol
        if not can_sell_symbol(symbol_s, default=False):
            logger.warning(
                "[SUMMARY AI SELL CREDIT PREFILTER] SELL candidate removed before approved symbol=%s reason=sell_credit_guard_ng",
                symbol_s,
            )
            return True, {"reason": "sell_credit_guard_ng"}
    except Exception:
        logger.exception("[SUMMARY AI SELL CREDIT PREFILTER] guard failed fail-open symbol=%s", symbol_s)
        return False, None

    return False, None


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from trading.entry.summary_ai import executor
        old_fn = getattr(executor, "_is_sell_reject_cached", None)
        if callable(old_fn) and getattr(old_fn, "_sell_credit_prefilter_patch_v1", False):
            _INSTALLED = True
            return True
        _is_sell_reject_or_credit_ng._sell_credit_prefilter_patch_v1 = True  # type: ignore[attr-defined]
        _is_sell_reject_or_credit_ng._original = old_fn  # type: ignore[attr-defined]
        executor._is_sell_reject_cached = _is_sell_reject_or_credit_ng
        _INSTALLED = True
        logger.warning("[SUMMARY AI SELL CREDIT PREFILTER] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI SELL CREDIT PREFILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI SELL CREDIT PREFILTER] auto install failed")

__all__ = ["install"]
