# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_entry_pipeline_range_prefilter_repair_patch.py
# Version: V1-SUMMARY-AI-PREFILTER-HIGHLOW-WIDE-REPAIR
# ------------------------------------------------------------
# Purpose:
#   final_board_guard_signature_compat_patch の SUMMARY_AI low-move prefilter が
#   row['high'] / row['low'] = close の flat 行を先に採用して、
#   row['high_price'] / row['low_price'] / day_high / day_low を見ずに
#   LOW_MOVE扱いで落とす問題を補正する。
#
# Important:
#   - 低変動ガードは緩和しない。
#   - 参照する high/low 候補を広げ、最も広い妥当レンジを使うだけ。
#   - それでも range_pct < min_range なら従来通り落とす。
# ============================================================
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-PREFILTER-HIGHLOW-WIDE-REPAIR"
_INSTALLED = False


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _to_dict(row: Any) -> dict[str, Any]:
    try:
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            return dict(d) if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _first_positive(d: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        v = _safe_float(d.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _pick_widest_range(d: dict[str, Any]) -> tuple[float, float, str]:
    pairs = [
        (("high",), ("low",), "high_low"),
        (("high_price", "HighPrice"), ("low_price", "LowPrice"), "high_price_low_price"),
        (("day_high", "DayHigh"), ("day_low", "DayLow"), "day_high_day_low"),
        (("intraday_high", "session_high", "today_high", "range_high"), ("intraday_low", "session_low", "today_low", "range_low"), "intraday_high_low"),
        (("opening_high", "high_1m_max", "recent_high"), ("opening_low", "low_1m_min", "recent_low"), "recent_high_low"),
    ]
    best_h = 0.0
    best_l = 0.0
    best_label = "missing"
    best_width = -1.0
    for high_keys, low_keys, label in pairs:
        h = _first_positive(d, high_keys)
        l = _first_positive(d, low_keys)
        if h > 0 and l > 0 and h >= l:
            width = h - l
            if width > best_width:
                best_h, best_l, best_label, best_width = h, l, label, width
    return best_h, best_l, best_label


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import core.startup.final_board_guard_signature_compat_patch as compat
        cur = getattr(compat, "_range_pct_from_row", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI RANGE PREFILTER REPAIR] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_prefilter_wide_range_v1", False):
            _INSTALLED = True
            return True

        original = cur

        def _range_pct_from_row_wide(row: Any):
            d = _to_dict(row)
            close = _safe_float(d.get("close") or d.get("close_price") or d.get("price") or d.get("current_price"), 0.0)
            symbol = str(d.get("symbol") or d.get("Symbol") or "")
            high, low, method = _pick_widest_range(d)
            if close <= 0:
                close = max(high, low, 1.0)
            if high <= 0 or low <= 0 or high < low:
                return original(row)
            ratio = float((high - low) / close) if close > 0 else 0.0
            old_ratio, old_close, old_high, old_low, old_symbol = original(row)
            # Only replace when the original chose a flat/narrow range and wider valid data exists.
            if ratio > float(old_ratio or 0.0):
                logger.warning(
                    "[SUMMARY AI RANGE PREFILTER REPAIR] widened symbol=%s method=%s old_high=%.4f old_low=%.4f old_range=%.6f new_high=%.4f new_low=%.4f new_range=%.6f version=%s",
                    symbol or old_symbol,
                    method,
                    _safe_float(old_high),
                    _safe_float(old_low),
                    _safe_float(old_ratio),
                    high,
                    low,
                    ratio,
                    VERSION,
                )
                return ratio, close, high, low, symbol or old_symbol
            return old_ratio, old_close, old_high, old_low, old_symbol

        _range_pct_from_row_wide._summary_ai_prefilter_wide_range_v1 = True  # type: ignore[attr-defined]
        _range_pct_from_row_wide._original = original  # type: ignore[attr-defined]
        compat._range_pct_from_row = _range_pct_from_row_wide
        _INSTALLED = True
        logger.warning("[SUMMARY AI RANGE PREFILTER REPAIR] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI RANGE PREFILTER REPAIR] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RANGE PREFILTER REPAIR] auto install failed")


__all__ = ["install", "VERSION"]
