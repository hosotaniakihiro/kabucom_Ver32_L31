from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False

SYMBOL_REASONS = {
    "SYMBOL_DAILY_ENTRY_LIMIT",
    "SYMBOL_STOP_AFTER_FIRST_LOSS",
    "SYMBOL_DAILY_LOSS_LIMIT",
}
GLOBAL_REASONS = {
    "GLOBAL_DAILY_TRADE_LIMIT",
    "GLOBAL_DAILY_LOSS_LIMIT",
    "GLOBAL_CONSECUTIVE_LOSS_LIMIT",
}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _norm_side(v: Any) -> str:
    s = str(v or "BUY").strip().upper()
    return s if s in {"BUY", "SELL"} else "BUY"


def _symbol_only_daily_risk_block_reason(symbol: str, side: str) -> Tuple[bool, str, Dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_PRE_FILTER_DAILY_RISK", True):
        return False, "", {}
    symbol_s = _norm_symbol(symbol)
    side_s = _norm_side(side)
    if not symbol_s:
        return False, "", {}
    try:
        from core.startup import entry_daily_risk_runtime_patch as daily_risk
        fn = getattr(daily_risk, "_risk_block_reason", None)
        if not callable(fn):
            return False, "", {}
        blocked, reason, detail = fn(symbol_s, side_s)
        reason_s = str(reason or "")
        if not blocked:
            return False, "", {}
        if not isinstance(detail, dict):
            detail = {"detail": str(detail)}
        if reason_s in SYMBOL_REASONS:
            return True, reason_s, detail
        if reason_s in GLOBAL_REASONS:
            logger.warning(
                "[SUMMARY AI SYMBOL RISK PATCH] ignore global reason before approved symbol=%s side=%s reason=%s detail=%s",
                symbol_s, side_s, reason_s, detail,
            )
            return False, "", {}
        logger.warning(
            "[SUMMARY AI SYMBOL RISK PATCH] unknown reason fail-open symbol=%s side=%s reason=%s detail=%s",
            symbol_s, side_s, reason_s, detail,
        )
        return False, "", {}
    except Exception:
        logger.exception("[SUMMARY AI SYMBOL RISK PATCH] failed symbol=%s side=%s", symbol_s, side_s)
        return False, "", {}


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from trading.entry.summary_ai import executor
        old_fn = getattr(executor, "_daily_risk_block_reason", None)
        if callable(old_fn) and getattr(old_fn, "_symbol_only_patch_v1", False):
            _INSTALLED = True
            return True
        _symbol_only_daily_risk_block_reason._symbol_only_patch_v1 = True  # type: ignore[attr-defined]
        _symbol_only_daily_risk_block_reason._original = old_fn  # type: ignore[attr-defined]
        executor._daily_risk_block_reason = _symbol_only_daily_risk_block_reason
        _INSTALLED = True
        logger.warning("[SUMMARY AI SYMBOL RISK PATCH] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI SYMBOL RISK PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI SYMBOL RISK PATCH] auto install failed")

__all__ = ["install"]
