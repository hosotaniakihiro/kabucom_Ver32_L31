# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-RANGE-CONSISTENCY"
_INSTALLED = False
_ORIG = None


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        try:
            x = _f(d.get(k), 0.0)
            if x > 0:
                return x
        except Exception:
            pass
    return 0.0


def _ratio(close: float, high: float, low: float) -> float:
    if close <= 0 or high <= 0 or low <= 0 or high < low:
        return 0.0
    return max(0.0, (high - low) / close)


def _best_range(row: dict[str, Any], close: float) -> tuple[str, float, float, float]:
    cond = row.get("entry_conditions") if isinstance(row.get("entry_conditions"), dict) else {}
    candidates: list[tuple[str, float, float, float]] = []

    for label, src in (("bar", row), ("row_day", row), ("conditions_day", cond), ("recent", row)):
        if label == "bar":
            hi = _first(src, ("high_price", "high"))
            lo = _first(src, ("low_price", "low"))
        elif label == "recent":
            hi = _first(src, ("recent_high", "range_high", "high_1m_max"))
            lo = _first(src, ("recent_low", "range_low", "low_1m_min"))
        else:
            hi = _first(src, ("day_high", "today_high", "session_high", "intraday_high", "HighPrice"))
            lo = _first(src, ("day_low", "today_low", "session_low", "intraday_low", "LowPrice"))
        if hi > 0 and lo > 0:
            h, l = max(hi, lo), min(hi, lo)
            candidates.append((label, h, l, _ratio(close, h, l)))

    explicit = max(
        _first(row, ("range_pct", "intraday_range_pct", "summary_ai_range_repair_pct")),
        _first(cond, ("range_pct", "intraday_range_pct", "day_range_pct")),
    )
    if explicit > 0:
        width = close * explicit
        candidates.append(("explicit", close + width / 2.0, max(0.01, close - width / 2.0), explicit))

    if not candidates:
        return ("missing", 0.0, 0.0, 0.0)
    return max(candidates, key=lambda x: x[3])


def install() -> bool:
    global _INSTALLED, _ORIG
    if _INSTALLED:
        return True
    try:
        from trading.handlers import entry_order_builder as eob
        cur = getattr(eob, "_low_move_hard_block", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_range_consistency_v1", False):
            _INSTALLED = True
            return True
        _ORIG = getattr(cur, "_original", cur)

        def _patched(entry_row, *, symbol: str, source: str):
            result = _ORIG(entry_row, symbol=symbol, source=source)
            if result is None:
                return None
            try:
                if str(source or "").upper() != "SUMMARY_AI" or not isinstance(entry_row, dict):
                    return result
                reason = str(result.get("reason") if isinstance(result, dict) else "").upper()
                if reason not in {"LOW_MOVE_RANGE_TOO_SMALL", "LOW_MOVE_ATR_TOO_SMALL"}:
                    return result
                close = _first(entry_row, ("close_price", "close", "price", "current_price"))
                if close <= 0:
                    return result
                min_range = _f(getattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", 0.006), 0.006)
                min_atr = _f(getattr(eob, "ENTRY_ORDER_MIN_ATR_RATIO", 0.0035), 0.0035)
                label, high, low, rpct = _best_range(entry_row, close)
                width = max(0.0, high - low)
                atr0 = _first(entry_row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"))
                atr = max(atr0, width)
                atr_ratio = atr / close if close > 0 and atr > 0 else 0.0
                if rpct >= min_range and atr_ratio >= min_atr:
                    entry_row["high"] = high
                    entry_row["high_price"] = high
                    entry_row["low"] = low
                    entry_row["low_price"] = low
                    entry_row["range_pct"] = rpct
                    entry_row["intraday_range_pct"] = rpct
                    entry_row["atr"] = atr
                    entry_row["atr_1m"] = atr
                    entry_row["ATR"] = atr
                    logger.warning(
                        "[SUMMARY AI RANGE CONSISTENCY] allow symbol=%s reason=%s source=%s high=%.2f low=%.2f range_pct=%.6f atr_ratio=%.6f min_range=%.6f min_atr=%.6f version=%s",
                        symbol, reason, label, high, low, rpct, atr_ratio, min_range, min_atr, VERSION,
                    )
                    return None
                return result
            except Exception:
                logger.exception("[SUMMARY AI RANGE CONSISTENCY] failed symbol=%s version=%s", symbol, VERSION)
                return result

        _patched._summary_ai_range_consistency_v1 = True  # type: ignore[attr-defined]
        _patched._original = _ORIG  # type: ignore[attr-defined]
        eob._low_move_hard_block = _patched
        _INSTALLED = True
        logger.warning("[SUMMARY AI RANGE CONSISTENCY] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI RANGE CONSISTENCY] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RANGE CONSISTENCY] auto install failed")


__all__ = ["install", "VERSION"]
