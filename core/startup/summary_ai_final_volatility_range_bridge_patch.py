# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_final_volatility_range_bridge_patch.py
# Version: V1-SUMMARY-AI-FINAL-VOL-RANGE-BRIDGE
# ------------------------------------------------------------
# SUMMARY_AI は候補生成側で range_pct/day_high/day_low を補完済みでも、
# final entry_controller -> volatility_filter の entry_row 判定では
# high/low だけを見て 1円幅扱いになり snapshot_no_order で止まることがある。
#
# 例:
#   approved row: range_pct=0.0373 / high=2815 / low=2710
#   final filter: high=2815 / low=2814 -> ratio=0.000355 -> NG
#
# このpatchは SUMMARY_AI/SUMMARY/PUSH の entry_row に限り、既存の
# range_pct / intraday_range_pct / day_high-day_low を final volatility filterへ
#橋渡しする。低変動銘柄を無条件通過させる fail-open ではない。
# ============================================================
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-FINAL-VOL-RANGE-BRIDGE"
_INSTALLED = False


def _row_dict(row: Any) -> dict[str, Any]:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return dict(d)
    except Exception:
        pass
    return {}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _first(row: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        try:
            v = row.get(name)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _symbol(row: dict[str, Any]) -> str:
    s = str(_first(row, ("symbol", "Symbol", "code", "stock_code"), "") or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _is_summary_ai(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(k) or "") for k in (
        "source", "entry_source", "entry_type", "pipeline_source", "reason", "ai_reason", "model_used"
    )).upper()
    return any(x in text for x in ("SUMMARY_AI", "SUMMARY", "PUSH_SUMMARY", "SRC=SUMMARY"))


def _range_ratio_from_row(row: dict[str, Any]) -> tuple[float, str, float, float, float]:
    close = _num(_first(row, ("close_price", "close", "price", "current_price", "last_price"), 0), 0.0)
    high = _num(_first(row, ("high_price", "high", "day_high", "today_high", "intraday_high", "range_high"), 0), 0.0)
    low = _num(_first(row, ("low_price", "low", "day_low", "today_low", "intraday_low", "range_low"), 0), 0.0)

    best = 0.0
    method = "missing"
    if close > 0 and high > 0 and low > 0 and high >= low:
        best = max(best, (high - low) / close)
        method = "high_low"

    for name in ("range_pct", "intraday_range_pct", "day_range_pct", "display_range_pct"):
        val = _num(row.get(name), 0.0)
        if val <= 0:
            continue
        # 0.0373 と 3.73 の両方を受ける。
        ratio = val / 100.0 if val > 1.0 else val
        if ratio > best:
            best = ratio
            method = name

    return best, method, close, high, low


def _summary_ai_range_ok(entry_row: Any, min_pct: float, label: str) -> bool | None:
    row = _row_dict(entry_row)
    if not row or not _is_summary_ai(row):
        return None
    ratio, method, close, high, low = _range_ratio_from_row(row)
    ok = bool(ratio >= float(min_pct))
    logger.warning(
        "[SUMMARY AI FINAL VOL RANGE BRIDGE] %s symbol=%s ok=%s ratio=%.6f min_pct=%.6f method=%s close=%.2f high=%.2f low=%.2f version=%s",
        label,
        _symbol(row),
        ok,
        ratio,
        float(min_pct),
        method,
        close,
        high,
        low,
        VERSION,
    )
    if ok:
        return True
    return None


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.filters.volatility_filter as vf

        orig_entry = getattr(vf, "_entry_row_range_ok", None)
        orig_range = getattr(vf, "_range_5m_filter_from_entry_row", None)
        if not callable(orig_entry) or not callable(orig_range):
            logger.warning("[SUMMARY AI FINAL VOL RANGE BRIDGE] target missing entry=%s range=%s version=%s", callable(orig_entry), callable(orig_range), VERSION)
            return False
        if getattr(orig_entry, "_summary_ai_final_vol_range_bridge_v1", False):
            _INSTALLED = True
            return True

        base_entry = getattr(orig_entry, "_original", orig_entry)
        base_range = getattr(orig_range, "_original", orig_range)

        def _patched_entry_row_range_ok(entry_row: Any, min_pct: float = None):
            if min_pct is None:
                min_pct = getattr(vf, "DEFAULT_ENTRY_ROW_RANGE_MIN_PCT", 0.006)
            bridged = _summary_ai_range_ok(entry_row, float(min_pct), "entry_row_range")
            if bridged is True:
                return True
            return base_entry(entry_row, min_pct=min_pct)

        def _patched_range_5m_filter_from_entry_row(entry_row: Any, min_pct: float = None):
            if min_pct is None:
                min_pct = getattr(vf, "DEFAULT_RANGE_5M_MIN_PCT", 0.012)
            bridged = _summary_ai_range_ok(entry_row, float(min_pct), "range_5m")
            if bridged is True:
                return True
            return base_range(entry_row, min_pct=min_pct)

        _patched_entry_row_range_ok._summary_ai_final_vol_range_bridge_v1 = True  # type: ignore[attr-defined]
        _patched_entry_row_range_ok._original = base_entry  # type: ignore[attr-defined]
        _patched_range_5m_filter_from_entry_row._summary_ai_final_vol_range_bridge_v1 = True  # type: ignore[attr-defined]
        _patched_range_5m_filter_from_entry_row._original = base_range  # type: ignore[attr-defined]

        vf._entry_row_range_ok = _patched_entry_row_range_ok
        vf._range_5m_filter_from_entry_row = _patched_range_5m_filter_from_entry_row
        _INSTALLED = True
        logger.warning("[SUMMARY AI FINAL VOL RANGE BRIDGE] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI FINAL VOL RANGE BRIDGE] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI FINAL VOL RANGE BRIDGE] auto install failed")


__all__ = ["install", "VERSION"]
