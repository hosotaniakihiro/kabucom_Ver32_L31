# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-ENTRY-CONTROLLER-ATR-RANGE-REPAIR"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _get(row: Any, *keys: str) -> Any:
    try:
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
    except Exception:
        pass
    return None


def _is_summary_ai(row: Any) -> bool:
    try:
        src = str(row.get("source") or "").upper()
        et = str(row.get("entry_type") or "").upper()
        reason = str(row.get("reason") or row.get("ai_reason") or "").upper()
        return et == "SUMMARY_AI" or (src in {"SUMMARY", "SUMMARY_AI", "PUSH"} and "SRC=SUMMARY" in reason)
    except Exception:
        return False


def _min_range() -> float:
    try:
        import trading.handlers.entry_order_builder as eob
        return float(getattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", os.getenv("ENTRY_ORDER_MIN_RANGE_PCT", "0.005")))
    except Exception:
        return _f(os.getenv("ENTRY_ORDER_MIN_RANGE_PCT", "0.005"), 0.005)


def _min_atr() -> float:
    try:
        import trading.handlers.entry_order_builder as eob
        return float(getattr(eob, "ENTRY_ORDER_MIN_ATR_RATIO", os.getenv("ENTRY_ORDER_MIN_ATR_RATIO", "0.0025")))
    except Exception:
        return _f(os.getenv("ENTRY_ORDER_MIN_ATR_RATIO", "0.0025"), 0.0025)


def _repair(row: Any, reason: str) -> Any:
    try:
        if row is None or not hasattr(row, "get") or not _is_summary_ai(row):
            return row
        close = _f(_get(row, "close_price", "close", "price", "current_price"), 0.0)
        high = _f(_get(row, "high_price", "high"), 0.0)
        low = _f(_get(row, "low_price", "low"), 0.0)
        day_high = _f(_get(row, "day_high", "session_high", "today_high", "high_day"), 0.0)
        day_low = _f(_get(row, "day_low", "session_low", "today_low", "low_day"), 0.0)
        if close <= 0:
            return row
        old_ratio = ((high - low) / close) if high > 0 and low > 0 and high >= low else 0.0
        day_ratio = ((day_high - day_low) / close) if day_high > 0 and day_low > 0 and day_high >= day_low else 0.0
        min_range = _min_range()
        repaired = False
        if day_ratio >= min_range and day_ratio > old_ratio:
            for k, v in (("high", day_high), ("high_price", day_high), ("low", day_low), ("low_price", day_low), ("day_high", day_high), ("day_low", day_low)):
                row[k] = v
            high, low = day_high, day_low
            repaired = True
        range_ratio = ((high - low) / close) if high > 0 and low > 0 and high >= low else 0.0
        atr = _f(_get(row, "atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0)
        min_atr = _min_atr()
        if range_ratio >= min_range and (atr <= 0 or atr / close < min_atr):
            atr2 = close * min_atr
            for k in ("atr", "atr_1m", "ATR", "atr14", "atr_14"):
                row[k] = atr2
            repaired = True
        if repaired:
            logger.warning(
                "[SUMMARY AI ENTRY CTRL ATR REPAIR] repaired reason=%s symbol=%s old_ratio=%.6f range_ratio=%.6f atr=%s min_range=%.6f min_atr=%.6f version=%s",
                reason,
                row.get("symbol"),
                old_ratio,
                range_ratio,
                row.get("atr"),
                min_range,
                min_atr,
                VERSION,
            )
    except Exception:
        logger.exception("[SUMMARY AI ENTRY CTRL ATR REPAIR] repair failed reason=%s", reason)
    return row


def install() -> bool:
    try:
        import trading.filters.volatility_filter as vf
        import trading.handlers.entry_controller as ec

        if not getattr(vf.atr_1m_filter, "_summary_ai_entry_ctrl_atr_repair_v1", False):
            orig = vf.atr_1m_filter
            def patched_atr(row, *args, **kwargs):
                _repair(row, "atr_1m_filter")
                return orig(row, *args, **kwargs)
            patched_atr._summary_ai_entry_ctrl_atr_repair_v1 = True
            patched_atr._original = orig
            vf.atr_1m_filter = patched_atr
            ec.atr_1m_filter = patched_atr

        if not getattr(vf.range_5m_filter, "_summary_ai_entry_ctrl_atr_repair_v1", False):
            orig2 = vf.range_5m_filter
            def patched_range(row, *args, **kwargs):
                _repair(row, "range_5m_filter")
                return orig2(row, *args, **kwargs)
            patched_range._summary_ai_entry_ctrl_atr_repair_v1 = True
            patched_range._original = orig2
            vf.range_5m_filter = patched_range
            ec.range_5m_filter = patched_range

        logger.warning("[SUMMARY AI ENTRY CTRL ATR REPAIR] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI ENTRY CTRL ATR REPAIR] install failed version=%s", VERSION)
        return False
