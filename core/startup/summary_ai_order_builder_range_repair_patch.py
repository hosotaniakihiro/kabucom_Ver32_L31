# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_order_builder_range_repair_patch.py
# Version: V5-SUMMARY-AI-ATR-REPAIR-MULTI-SOURCE-HISTORY
# ------------------------------------------------------------
# SUMMARY_AI の直接スナップショット経路で row が
# high == low == close / atr == 0 のまま entry_controller と
# entry_order_builder に渡り、ATR_1M_FILTER_NG / LOW_MOVE_RANGE_TOO_SMALL /
# LOW_MOVE_NO_ATR / LOW_MOVE_ATR_TOO_SMALL で落ちる問題を補正する。
#
# 重要:
#   - 閾値は緩和しない。
#   - fail-open しない。
#   - day_high/day_low または global_context の履歴でレンジが確認できる時だけ補完する。
#
# V5:
#   - global_context の push だけでなく summary / legacy / ranking / push-cache も検索。
#   - 1分履歴が複数本ある場合は ATR(14) を計算して atr_1m を補完。
# ============================================================
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V5-SUMMARY-AI-ATR-REPAIR-MULTI-SOURCE-HISTORY"
_INSTALLED = False
_ORIGINAL_LOW_MOVE = None

_HISTORY_SOURCES = ("push", "summary", "legacy", "ranking", "push-cache")


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


def _atr_from_row(row: dict[str, Any]) -> float:
    return _first_pos(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"))


def _best_from_row(row: dict[str, Any]) -> tuple[float, float, float, float, str]:
    close = _first_pos(row, ("close_price", "close", "current_price", "price", "last_price"))
    atr = _atr_from_row(row)
    pairs = [
        (("high_price", "HighPrice"), ("low_price", "LowPrice"), "row_high_price_low_price"),
        (("high",), ("low",), "row_high_low"),
        (("day_high", "DayHigh", "today_high", "intraday_high", "session_high", "range_high"), ("day_low", "DayLow", "today_low", "intraday_low", "session_low", "range_low"), "row_day_high_low"),
        (("high_1m_max", "recent_high"), ("low_1m_min", "recent_low"), "row_recent_high_low"),
    ]
    best = (close, 0.0, 0.0, atr, "row_missing")
    best_ratio = 0.0
    for hk, lk, label in pairs:
        h = _first_pos(row, hk)
        l = _first_pos(row, lk)
        ratio = _range_ratio(close, h, l)
        if ratio > best_ratio:
            best = (close, h, l, atr, label)
            best_ratio = ratio
    return best


def _normalize_symbol(s: Any) -> str:
    try:
        x = str(s or "").strip()
        if x.endswith(".0") and x[:-2].isdigit():
            return x[:-2]
        return x
    except Exception:
        return ""


def _latest_symbol_row_from_df(df: Any, symbol: str) -> dict[str, Any]:
    try:
        import pandas as pd
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return {}
        s = df[df["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip() == str(symbol)]
        if s.empty:
            return {}
        for col in ("datetime", "end_time", "updated_at", "last_update", "time"):
            if col in s.columns:
                try:
                    s = s.assign(_dt=pd.to_datetime(s[col], errors="coerce")).sort_values("_dt")
                    break
                except Exception:
                    pass
        return s.iloc[-1].to_dict()
    except Exception:
        return {}


def _atr14_from_symbol_history(df: Any, symbol: str) -> tuple[float, int, str]:
    try:
        import pandas as pd
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0.0, 0, "not_df"
        s = df[df["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip() == str(symbol)].copy()
        if s.empty:
            return 0.0, 0, "symbol_missing"
        aliases = {
            "high_price": ("high_price", "high", "High", "h"),
            "low_price": ("low_price", "low", "Low", "l"),
            "close_price": ("close_price", "close", "Close", "price", "current_price", "c"),
        }
        for dst, keys in aliases.items():
            if dst in s.columns:
                continue
            for k in keys:
                if k in s.columns:
                    s[dst] = s[k]
                    break
        if not {"high_price", "low_price", "close_price"}.issubset(set(s.columns)):
            return 0.0, len(s), "ohlc_missing"
        for col in ("high_price", "low_price", "close_price"):
            s[col] = pd.to_numeric(s[col], errors="coerce")
        for col in ("datetime", "end_time", "updated_at", "last_update", "time"):
            if col in s.columns:
                try:
                    s = s.assign(_dt=pd.to_datetime(s[col], errors="coerce")).sort_values("_dt")
                    s = s.drop_duplicates(subset=["_dt"], keep="last")
                    break
                except Exception:
                    pass
        s = s.dropna(subset=["high_price", "low_price", "close_price"])
        s = s[(s["high_price"] > 0) & (s["low_price"] > 0) & (s["close_price"] > 0)]
        bars = len(s)
        if bars < 15:
            return 0.0, bars, "bars_insufficient"
        highs = s["high_price"].to_list()
        lows = s["low_price"].to_list()
        closes = s["close_price"].to_list()
        tr: list[float] = []
        for i in range(1, bars):
            tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        if len(tr) < 14:
            return 0.0, bars, "tr_insufficient"
        return float(sum(tr[-14:]) / 14.0), bars, "atr14"
    except Exception:
        logger.debug("[SUMMARY AI RANGE REPAIR] atr14 calc failed symbol=%s", symbol, exc_info=True)
        return 0.0, 0, "exception"


def _history_frames_from_global_context(tf: int = 1) -> list[tuple[str, Any]]:
    frames: list[tuple[str, Any]] = []
    try:
        from core.global_context.context import global_context
    except Exception:
        try:
            from core.global_context import global_context  # type: ignore
        except Exception:
            return frames

    for getter_name, label in (("get_summary_history", "summary_history"), ("get_merged_summary", "merged_summary")):
        getter = getattr(global_context, getter_name, None)
        if not callable(getter):
            continue
        for src in _HISTORY_SOURCES:
            try:
                df = getter(tf, source=src)
                if getattr(df, "empty", True) is False:
                    frames.append((f"{label}:{src}", df))
            except Exception:
                logger.debug("[SUMMARY AI RANGE REPAIR] global lookup failed getter=%s source=%s", getter_name, src, exc_info=True)
    return frames


def _best_from_global_context(symbol: str) -> tuple[float, float, float, float, str]:
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return 0.0, 0.0, 0.0, 0.0, "symbol_missing"

    best = (0.0, 0.0, 0.0, 0.0, "gc_missing")
    best_ratio = 0.0
    for label, df in _history_frames_from_global_context(1):
        try:
            d = _latest_symbol_row_from_df(df, symbol)
            if not d:
                continue
            c, h, l, atr, method = _best_from_row(d)
            atr14, bars, atr_method = _atr14_from_symbol_history(df, symbol)
            atr = max(float(atr or 0.0), float(atr14 or 0.0))
            ratio = _range_ratio(c, h, l)
            if ratio > best_ratio or (ratio == best_ratio and atr > best[3]):
                best = (c, h, l, atr, f"{label}:{method}:{atr_method}:bars={bars}")
                best_ratio = ratio
        except Exception:
            logger.debug("[SUMMARY AI RANGE REPAIR] global source failed label=%s symbol=%s", label, symbol, exc_info=True)
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
    symbol = _normalize_symbol(symbol or row.get("symbol"))
    close0, high0, low0, atr0, method0 = _best_from_row(row)
    old_ratio = _range_ratio(close0, high0, low0)
    best = (close0, high0, low0, atr0, method0)
    best_ratio = old_ratio

    c2, h2, l2, atr2, method2 = _best_from_global_context(symbol)
    ratio2 = _range_ratio(c2, h2, l2)
    if ratio2 > best_ratio or (ratio2 == best_ratio and atr2 > best[3]):
        best = (c2, h2, l2, atr2, method2)
        best_ratio = ratio2

    out = dict(row)
    c, h, l, history_atr, method = best
    old_atr = max(_atr_from_row(out), atr0)
    min_range = _entry_min_range_pct()
    min_atr_ratio = _entry_min_atr_ratio()
    required_atr = c * min_atr_ratio if c > 0 else 0.0
    range_atr_proxy = max(0.0, h - l) if c > 0 and h >= l else 0.0
    effective_atr = max(old_atr, history_atr)

    diag = {
        "symbol": symbol,
        "source": source,
        "old_close": close0,
        "old_high": high0,
        "old_low": low0,
        "old_ratio": old_ratio,
        "old_atr": old_atr,
        "history_atr": history_atr,
        "new_close": c,
        "new_high": h,
        "new_low": l,
        "new_ratio": best_ratio,
        "min_range": min_range,
        "min_atr_ratio": min_atr_ratio,
        "required_atr": required_atr,
        "method": method,
        "repaired": False,
        "atr_repaired": False,
        "version": VERSION,
    }

    if c > 0 and h > 0 and l > 0 and h >= l and best_ratio > old_ratio:
        out.update({
            "close": c,
            "close_price": c,
            "current_price": c,
            "price": c,
            "high": h,
            "low": l,
            "high_price": h,
            "low_price": l,
            "day_high": max(_safe_float(out.get("day_high"), 0.0), h),
            "day_low": l if _safe_float(out.get("day_low"), 0.0) <= 0 else min(_safe_float(out.get("day_low"), l), l),
            "range_pct": best_ratio,
            "intraday_range_pct": best_ratio,
        })
        diag["repaired"] = True

    # ATR補完は「補正後レンジが十分ある」場合だけ。レンジ不足銘柄は従来通り止める。
    if c > 0 and best_ratio >= min_range:
        if effective_atr > 0 and (effective_atr / c) >= min_atr_ratio:
            new_atr = effective_atr
            method_atr = "row_or_history"
        elif range_atr_proxy >= required_atr > 0:
            new_atr = range_atr_proxy
            method_atr = "range_proxy"
        elif effective_atr > old_atr:
            new_atr = effective_atr
            method_atr = "history_atr_below_threshold"
        else:
            new_atr = 0.0
            method_atr = "missing"
        if new_atr > 0:
            out["atr"] = new_atr
            out["atr_1m"] = new_atr
            out["ATR"] = new_atr
            diag["atr_repaired"] = new_atr > old_atr or old_atr <= 0
            diag["new_atr"] = new_atr
            diag["atr_method"] = method_atr

    return out, diag


def _update_original_row(entry_row: Any, repaired: dict[str, Any]) -> None:
    try:
        if isinstance(entry_row, dict):
            for k in ("close", "close_price", "current_price", "price", "high", "low", "high_price", "low_price", "day_high", "day_low", "range_pct", "intraday_range_pct", "atr", "atr_1m", "ATR"):
                if k in repaired:
                    entry_row[k] = repaired[k]
    except Exception:
        pass


def _install_entry_controller_filter_repair() -> bool:
    try:
        import trading.filters.volatility_filter as vf
        import trading.handlers.entry_controller as ec

        if not getattr(vf.atr_1m_filter, "_summary_ai_entry_controller_repair_v5", False):
            orig_atr = getattr(vf.atr_1m_filter, "_original", vf.atr_1m_filter)

            def _patched_atr_1m_filter(entry_row, *args, **kwargs):
                row = _row_dict(entry_row)
                if _source_is_summary_ai(row.get("source"), row):
                    symbol = str(row.get("symbol") or "")
                    repaired, diag = _repair_row(entry_row, symbol=symbol, source=str(row.get("source") or "SUMMARY"))
                    if diag.get("repaired") or diag.get("atr_repaired"):
                        logger.warning("[SUMMARY AI ENTRY CTRL ATR REPAIR] repaired before atr_1m_filter detail=%s version=%s", diag, VERSION)
                        _update_original_row(entry_row, repaired)
                        return orig_atr(repaired, *args, **kwargs)
                return orig_atr(entry_row, *args, **kwargs)

            _patched_atr_1m_filter._summary_ai_entry_controller_repair_v4 = True  # type: ignore[attr-defined]
            _patched_atr_1m_filter._summary_ai_entry_controller_repair_v5 = True  # type: ignore[attr-defined]
            _patched_atr_1m_filter._original = orig_atr  # type: ignore[attr-defined]
            vf.atr_1m_filter = _patched_atr_1m_filter
            ec.atr_1m_filter = _patched_atr_1m_filter

        # range_5m_filter向けのSUMMARY_AI履歴補完(_repair_row呼び出し)は
        # trading/filters/volatility_filter.py の _range5m_repair_summary_ai_row
        # (_range_5m_filter_from_entry_row 本体、V6) へインライン化済み。
        # _repair_row 自体は entry_final_filter_failopen_patch.py も外部から
        # 呼び出す共有ライブラリのためここに残置する。

        logger.warning("[SUMMARY AI ENTRY CTRL ATR REPAIR] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI ENTRY CTRL ATR REPAIR] install failed version=%s", VERSION)
        return False


def install() -> bool:
    global _INSTALLED, _ORIGINAL_LOW_MOVE
    ctrl_ok = _install_entry_controller_filter_repair()
    if _INSTALLED:
        return True or ctrl_ok
    try:
        from trading.handlers import entry_order_builder as eob
        cur = getattr(eob, "_low_move_hard_block", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI ORDER RANGE REPAIR] target missing version=%s", VERSION)
            return bool(ctrl_ok)
        if getattr(cur, "_summary_ai_order_range_repair_v5", False):
            _INSTALLED = True
            return True

        _ORIGINAL_LOW_MOVE = getattr(cur, "_original", cur)

        def _patched_low_move_hard_block(entry_row, *, symbol: str, source: str):
            row = _row_dict(entry_row)
            if not _source_is_summary_ai(source, row):
                return _ORIGINAL_LOW_MOVE(entry_row, symbol=symbol, source=source)
            repaired, diag = _repair_row(entry_row, symbol=str(symbol or row.get("symbol") or ""), source=str(source or row.get("source") or ""))
            if diag.get("repaired") or diag.get("atr_repaired"):
                logger.warning("[SUMMARY AI ORDER RANGE REPAIR] repaired before strict low-move guard detail=%s version=%s", diag, VERSION)
                _update_original_row(entry_row, repaired)
                return _ORIGINAL_LOW_MOVE(repaired, symbol=symbol, source=source)
            return _ORIGINAL_LOW_MOVE(entry_row, symbol=symbol, source=source)

        _patched_low_move_hard_block._summary_ai_order_range_repair_v1 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._summary_ai_order_range_repair_v2 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._summary_ai_order_range_repair_v3 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._summary_ai_order_range_repair_v4 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._summary_ai_order_range_repair_v5 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._original = _ORIGINAL_LOW_MOVE  # type: ignore[attr-defined]
        eob._low_move_hard_block = _patched_low_move_hard_block
        _INSTALLED = True
        logger.warning("[SUMMARY AI ORDER RANGE REPAIR] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI ORDER RANGE REPAIR] install failed version=%s", VERSION)
        return bool(ctrl_ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ORDER RANGE REPAIR] auto install failed")


__all__ = ["install", "VERSION"]
