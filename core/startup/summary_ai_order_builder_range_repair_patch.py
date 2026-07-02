# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_order_builder_range_repair_patch.py
# Version: V2-SUMMARY-AI-ORDER-BUILDER-RANGE-AND-ATR-REPAIR
# ------------------------------------------------------------
# Purpose:
#   SUMMARY_AI の直接スナップショット経路で、entry_pipeline 側の prefilter は
#   通っても、entry_order_builder._low_move_hard_block() に渡る row が
#   high == low == close のままになり、LOW_MOVE_RANGE_TOO_SMALL で
#   実発注直前に落ちる問題を補正する。
#
# V2:
#   - 値幅は day_high/day_low で十分に補正できているのに、ATRが0/極小のまま
#     LOW_MOVE_NO_ATR / LOW_MOVE_ATR_TOO_SMALL で止まる問題を補正。
#   - 補正レンジが ENTRY_ORDER_MIN_RANGE_PCT を満たす場合だけ、ATRを
#     ENTRY_ORDER_MIN_ATR_RATIO 以上へ補完する。
#   - 低変動・ATRしきい値は緩和しない。欠損ATRの補完だけ。
#
# Important:
#   - 低変動ガードは緩和しない。
#   - 同一symbolの最新1分summary履歴 / merged summary / row内 day_high/day_low から
#     妥当なレンジを補完し、元の strict guard を再実行するだけ。
#   - 補完後も ENTRY_ORDER_MIN_RANGE_PCT 未満なら従来通り NG。
# ============================================================
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V2-SUMMARY-AI-ORDER-BUILDER-RANGE-AND-ATR-REPAIR"
_INSTALLED = False
_ORIGINAL = None


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


def _row_dict(row: Any) -> dict[str, Any]:
    try:
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            return dict(d) if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _first_pos(d: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        v = _safe_float(d.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _source_is_summary_ai(source: Any, row: dict[str, Any]) -> bool:
    src = str(source or row.get("source") or row.get("entry_type") or row.get("pipeline_source") or "").upper()
    text = " ".join(str(row.get(k) or "") for k in ("source", "entry_type", "reason", "ai_reason", "model_used")).upper()
    return src in {"SUMMARY_AI", "SUMMARY", "PUSH", "PUSH_SUMMARY"} or "SUMMARY_AI" in src or "SUMMARY_AI" in text or "SRC=SUMMARY" in text


def _range_ratio(close: float, high: float, low: float) -> float:
    try:
        if close <= 0 or high <= 0 or low <= 0 or high < low:
            return 0.0
        return max(0.0, (high - low) / close)
    except Exception:
        return 0.0


def _best_from_row(row: dict[str, Any]) -> tuple[float, float, float, str]:
    close = _first_pos(row, ("close_price", "close", "current_price", "price", "last_price"))
    pairs = [
        (("high_price", "HighPrice"), ("low_price", "LowPrice"), "row_high_price_low_price"),
        (("high",), ("low",), "row_high_low"),
        (("day_high", "DayHigh", "today_high", "intraday_high", "session_high", "range_high"), ("day_low", "DayLow", "today_low", "intraday_low", "session_low", "range_low"), "row_day_high_low"),
        (("high_1m_max", "recent_high"), ("low_1m_min", "recent_low"), "row_recent_high_low"),
    ]
    best = (close, 0.0, 0.0, "row_missing")
    best_ratio = 0.0
    for hk, lk, label in pairs:
        h = _first_pos(row, hk)
        l = _first_pos(row, lk)
        ratio = _range_ratio(close, h, l)
        if ratio > best_ratio:
            best = (close, h, l, label)
            best_ratio = ratio
    return best


def _latest_symbol_row_from_df(df: Any, symbol: str) -> dict[str, Any]:
    try:
        import pandas as pd
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return {}
        s = df[df["symbol"].astype(str) == str(symbol)]
        if s.empty:
            return {}
        for col in ("datetime", "end_time", "updated_at", "last_update"):
            if col in s.columns:
                try:
                    s = s.assign(_dt=pd.to_datetime(s[col], errors="coerce")).sort_values("_dt")
                    break
                except Exception:
                    pass
        return s.iloc[-1].to_dict()
    except Exception:
        return {}


def _best_from_global_context(symbol: str) -> tuple[float, float, float, str]:
    try:
        from core.global_context.context import global_context
    except Exception:
        try:
            from core.global_context import global_context  # type: ignore
        except Exception:
            return 0.0, 0.0, 0.0, "gc_import_failed"

    candidates: list[tuple[float, float, float, str]] = []
    for getter_name, label in (("get_summary_history", "summary_history_1m"), ("get_merged_summary", "merged_summary_1m")):
        try:
            getter = getattr(global_context, getter_name, None)
            if not callable(getter):
                continue
            df = getter(1, source="push")
            d = _latest_symbol_row_from_df(df, symbol)
            if not d:
                continue
            c, h, l, method = _best_from_row(d)
            candidates.append((c, h, l, f"{label}:{method}"))
        except Exception:
            logger.debug("[SUMMARY AI ORDER RANGE REPAIR] global lookup failed getter=%s symbol=%s", getter_name, symbol, exc_info=True)
    best = (0.0, 0.0, 0.0, "gc_missing")
    best_ratio = 0.0
    for c, h, l, label in candidates:
        ratio = _range_ratio(c, h, l)
        if ratio > best_ratio:
            best = (c, h, l, label)
            best_ratio = ratio
    return best


def _entry_min_range_pct() -> float:
    try:
        from trading.handlers import entry_order_builder as eob
        return float(getattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", 0.006) or 0.006)
    except Exception:
        return 0.006


def _entry_min_atr_ratio() -> float:
    try:
        from trading.handlers import entry_order_builder as eob
        return float(getattr(eob, "ENTRY_ORDER_MIN_ATR_RATIO", 0.0035) or 0.0035)
    except Exception:
        return 0.0035


def _repair_row(entry_row: Any, *, symbol: str, source: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = _row_dict(entry_row)
    close0, high0, low0, method0 = _best_from_row(row)
    old_ratio = _range_ratio(close0, high0, low0)
    best = (close0, high0, low0, method0)
    best_ratio = old_ratio

    c2, h2, l2, method2 = _best_from_global_context(symbol)
    ratio2 = _range_ratio(c2, h2, l2)
    if ratio2 > best_ratio:
        best = (c2, h2, l2, method2)
        best_ratio = ratio2

    out = dict(row)
    c, h, l, method = best
    old_atr = _first_pos(out, ("atr_1m", "atr", "ATR", "atr14", "atr_14"))
    min_range = _entry_min_range_pct()
    min_atr_ratio = _entry_min_atr_ratio()
    repaired_atr = False
    diag = {
        "symbol": symbol,
        "source": source,
        "old_close": close0,
        "old_high": high0,
        "old_low": low0,
        "old_ratio": old_ratio,
        "old_atr": old_atr,
        "new_close": c,
        "new_high": h,
        "new_low": l,
        "new_ratio": best_ratio,
        "min_range": min_range,
        "min_atr_ratio": min_atr_ratio,
        "method": method,
        "repaired": False,
        "atr_repaired": False,
    }
    if c > 0 and h > 0 and l > 0 and h >= l and best_ratio > old_ratio:
        out["close"] = c
        out["close_price"] = c
        out["current_price"] = c
        out["price"] = c
        out["high"] = h
        out["low"] = l
        out["high_price"] = h
        out["low_price"] = l
        out["day_high"] = max(_safe_float(out.get("day_high"), 0.0), h)
        out["day_low"] = l if _safe_float(out.get("day_low"), 0.0) <= 0 else min(_safe_float(out.get("day_low"), l), l)
        out["range_pct"] = best_ratio
        out["intraday_range_pct"] = best_ratio
        diag["repaired"] = True

    if c > 0 and best_ratio >= min_range:
        required_atr = c * min_atr_ratio
        # ATRが欠損/0/しきい値未満の時だけ補完する。レンジがしきい値未満なら補完しない。
        if old_atr <= 0 or (old_atr / c) < min_atr_ratio:
            repaired_atr = True
            out["atr"] = max(old_atr, required_atr)
            out["atr_1m"] = max(old_atr, required_atr)
            out["ATR"] = max(old_atr, required_atr)
            diag["atr_repaired"] = True
            diag["new_atr"] = out["atr"]

    return out, diag


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    try:
        from trading.handlers import entry_order_builder as eob
        cur = getattr(eob, "_low_move_hard_block", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI ORDER RANGE REPAIR] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_order_range_repair_v2", False):
            _INSTALLED = True
            return True

        _ORIGINAL = getattr(cur, "_original", cur)

        def _patched_low_move_hard_block(entry_row, *, symbol: str, source: str):
            row = _row_dict(entry_row)
            if not _source_is_summary_ai(source, row):
                return _ORIGINAL(entry_row, symbol=symbol, source=source)
            repaired, diag = _repair_row(entry_row, symbol=str(symbol or row.get("symbol") or ""), source=str(source or row.get("source") or ""))
            if diag.get("repaired") or diag.get("atr_repaired"):
                logger.warning("[SUMMARY AI ORDER RANGE REPAIR] repaired before strict low-move guard detail=%s version=%s", diag, VERSION)
                try:
                    if isinstance(entry_row, dict):
                        entry_row.update({k: repaired[k] for k in ("close", "close_price", "current_price", "price", "high", "low", "high_price", "low_price", "day_high", "day_low", "range_pct", "intraday_range_pct", "atr", "atr_1m", "ATR") if k in repaired})
                except Exception:
                    pass
                return _ORIGINAL(repaired, symbol=symbol, source=source)
            return _ORIGINAL(entry_row, symbol=symbol, source=source)

        _patched_low_move_hard_block._summary_ai_order_range_repair_v1 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._summary_ai_order_range_repair_v2 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._original = _ORIGINAL  # type: ignore[attr-defined]
        eob._low_move_hard_block = _patched_low_move_hard_block
        _INSTALLED = True
        logger.warning("[SUMMARY AI ORDER RANGE REPAIR] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI ORDER RANGE REPAIR] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ORDER RANGE REPAIR] auto install failed")


__all__ = ["install", "VERSION"]
