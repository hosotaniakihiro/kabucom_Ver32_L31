# ============================================================
# File   : core/startup/summary_ai_liquidity_runtime_patch.py
# Version: V1.0-SUMMARY-AI-PRE-APPROVED-LIQUIDITY-GUARD
# ------------------------------------------------------------
# 目的:
#   SUMMARY_AI の approved_rows 作成前に、出来高/売買代金の足切りを入れる。
#   entry_controller 発注直前パッチだけでは通らない経路をここで止める。
#
# default:
#   SUMMARY_AI_LIQ_MIN_VOLUME=30000
#   SUMMARY_AI_LIQ_MIN_TURNOVER_YEN=10000000
#   SUMMARY_AI_LIQ_MIN_PRICE=200
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return float(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _pick(d: Dict[str, Any], names: list[str]) -> Any:
    for n in names:
        if n in d and d.get(n) not in (None, ""):
            return d.get(n)
    return None


def _merged_item(item: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in ("source_row", "ai_row"):
        v = item.get(k)
        if isinstance(v, dict):
            out.update(v)
    out.update(item)
    return out


def _liquidity_ok(item: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
    row = _merged_item(item)
    symbol = _sym(_pick(row, ["symbol", "code", "stock_code"]))
    close = _f(_pick(row, ["close_price", "close", "price", "current_price"]), 0.0)
    volume = _f(_pick(row, ["volume", "Volume", "vol", "出来高"]), 0.0)
    turnover = _f(_pick(row, ["turnover", "turnover_yen", "trading_value", "売買代金"]), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume

    min_price = _env_float("SUMMARY_AI_LIQ_MIN_PRICE", 200.0)
    min_volume = _env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 10000000.0)

    detail = {
        "symbol": symbol,
        "close": close,
        "volume": volume,
        "turnover": turnover,
        "min_price": min_price,
        "min_volume": min_volume,
        "min_turnover": min_turnover,
    }
    if close < min_price:
        return False, "SUMMARY_AI_LIQ_PRICE_LOW", detail
    if volume < min_volume:
        return False, "SUMMARY_AI_LIQ_VOLUME_LOW", detail
    if turnover < min_turnover:
        return False, "SUMMARY_AI_LIQ_TURNOVER_LOW", detail
    return True, "OK", detail


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry.summary_ai.executor as ex
    except Exception:
        logger.exception("[SUMMARY AI LIQ GUARD] import executor failed")
        return False

    old = getattr(ex, "_filter_blocked_ai_ok_items", None)
    if not callable(old):
        logger.warning("[SUMMARY AI LIQ GUARD] _filter_blocked_ai_ok_items missing")
        return False

    if not getattr(old, "_summary_ai_liq_wrapped_v1", False):
        def wrapped(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            base = old(ok_items)
            kept: List[Dict[str, Any]] = []
            skipped: List[Dict[str, Any]] = []
            for item in base:
                if not isinstance(item, dict):
                    continue
                ok, reason, detail = _liquidity_ok(item)
                if ok:
                    kept.append(item)
                else:
                    skipped.append({"reason": reason, **detail})
            if skipped:
                logger.warning(
                    "[SUMMARY AI LIQ GUARD] filtered before approved rows before=%s after=%s skipped=%s",
                    len(base), len(kept), skipped[:50],
                )
            return kept
        wrapped._summary_ai_liq_wrapped_v1 = True  # type: ignore[attr-defined]
        wrapped._original = old  # type: ignore[attr-defined]
        ex._filter_blocked_ai_ok_items = wrapped

    _INSTALLED = True
    logger.warning(
        "[SUMMARY AI LIQ GUARD] installed min_volume=%s min_turnover=%s min_price=%s",
        _env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0),
        _env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 10000000.0),
        _env_float("SUMMARY_AI_LIQ_MIN_PRICE", 200.0),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI LIQ GUARD] auto install failed")

__all__ = ["install"]
