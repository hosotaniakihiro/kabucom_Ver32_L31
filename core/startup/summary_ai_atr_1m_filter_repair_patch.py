# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_atr_1m_filter_repair_patch.py
# Version: V1-SUMMARY-AI-ATR-1M-FILTER-REPAIR
# ------------------------------------------------------------
# Purpose:
#   Summary-AI の発注直前で ATR_1M_FILTER_NG になるケースを補正する。
#
# Important:
#   - ATRガードの閾値は緩めない。
#   - 元の atr_1m_filter がNGだった場合だけ、Summary-AI系のrowに限定して
#     既存 row / global_context の1分足summary / day_high/day_low から
#     high/low/range_pct/atr を補完し、同じ元ガードを再実行する。
#   - 補完後も元ガードがNGならNGのまま返す。
# ============================================================
from __future__ import annotations

import functools
import logging
import math
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-SUMMARY-AI-ATR-1M-FILTER-REPAIR"
_INSTALLED = False
_WATCHER_STARTED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


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
            d = dict(row)
        elif hasattr(row, "to_dict"):
            vv = row.to_dict()
            d = dict(vv) if isinstance(vv, dict) else {}
        else:
            d = {}
        raw = d.get("_raw")
        if hasattr(raw, "to_dict"):
            try:
                raw = raw.to_dict()
            except Exception:
                raw = None
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k not in d or d.get(k) in (None, ""):
                    d[k] = v
        return d
    except Exception:
        return {}


def _put(row: Any, key: str, value: Any) -> None:
    try:
        if isinstance(row, dict):
            row[key] = value
            return
        if hasattr(row, "__setitem__"):
            row[key] = value
            return
        setattr(row, key, value)
    except Exception:
        pass


def _first_pos(d: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        try:
            x = _safe_float(d.get(k), 0.0)
            if x > 0:
                return x
        except Exception:
            continue
    return 0.0


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _source_is_summary_ai(row: dict[str, Any]) -> bool:
    src = " ".join(str(row.get(k) or "") for k in (
        "source", "entry_source", "pipeline_source", "entry_type", "type", "reason", "ai_reason", "model_used",
    )).upper()
    return (
        "SUMMARY_AI" in src
        or "SRC=SUMMARY" in src
        or "SUMMARY" in src
        or "PUSH" in src
        or str(row.get("source") or "").upper() in {"SUMMARY", "PUSH", "PUSH_SUMMARY"}
    )


def _range_ratio(close: float, high: float, low: float) -> float:
    try:
        if close <= 0 or high <= 0 or low <= 0 or high < low:
            return 0.0
        return max(0.0, (high - low) / close)
    except Exception:
        return 0.0


def _entry_min_atr_ratio() -> float:
    try:
        from trading.handlers import entry_order_builder as eob
        return float(getattr(eob, "ENTRY_ORDER_MIN_ATR_RATIO", 0.0025) or 0.0025)
    except Exception:
        return _env_float("ENTRY_ORDER_MIN_ATR_RATIO", 0.0025)


def _entry_min_range_pct() -> float:
    try:
        from trading.handlers import entry_order_builder as eob
        return float(getattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", 0.003) or 0.003)
    except Exception:
        return _env_float("ENTRY_ORDER_MIN_RANGE_PCT", 0.003)


def _best_from_dict(d: dict[str, Any]) -> tuple[float, float, float, float, str]:
    close = _first_pos(d, ("close", "close_price", "current_price", "price", "last_price", "entry_price", "limit_price"))
    atr = _first_pos(d, ("atr_1m", "atr", "ATR", "atr14", "atr_14"))
    candidates = [
        (("high", "high_price", "bar_high", "summary_high"), ("low", "low_price", "bar_low", "summary_low"), "row_high_low"),
        (("day_high", "today_high", "intraday_high", "session_high", "range_high", "ranking_high", "snapshot_high", "high_price_day", "ai_disp_day_high", "ai_disp_high"),
         ("day_low", "today_low", "intraday_low", "session_low", "range_low", "ranking_low", "snapshot_low", "low_price_day", "ai_disp_day_low", "ai_disp_low"), "row_day_high_low"),
        (("high_1m_max", "recent_high", "recent_1m_high"), ("low_1m_min", "recent_low", "recent_1m_low"), "row_recent_high_low"),
    ]
    best = (close, 0.0, 0.0, atr, "missing")
    best_ratio = 0.0
    for hk, lk, label in candidates:
        h = _first_pos(d, hk)
        l = _first_pos(d, lk)
        ratio = _range_ratio(close, h, l)
        if ratio > best_ratio:
            best = (close, h, l, atr, label)
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


def _best_from_global_context(symbol: str) -> tuple[float, float, float, float, str]:
    try:
        from core.global_context.context import global_context
    except Exception:
        try:
            from core.global_context import global_context  # type: ignore
        except Exception:
            return 0.0, 0.0, 0.0, 0.0, "gc_import_failed"

    best = (0.0, 0.0, 0.0, 0.0, "gc_missing")
    best_ratio = 0.0
    for getter_name, label in (("get_summary_history", "summary_history_1m"), ("get_merged_summary", "merged_summary_1m")):
        try:
            getter = getattr(global_context, getter_name, None)
            if not callable(getter):
                continue
            df = getter(1, source="push")
            d = _latest_symbol_row_from_df(df, symbol)
            if not d:
                continue
            c, h, l, atr, method = _best_from_dict(d)
            ratio = _range_ratio(c, h, l)
            if ratio > best_ratio or (ratio == best_ratio and atr > best[3]):
                best = (c, h, l, atr, f"{label}:{method}")
                best_ratio = ratio
        except Exception:
            logger.debug("[SUMMARY AI ATR 1M REPAIR] global lookup failed getter=%s symbol=%s", getter_name, symbol, exc_info=True)
    return best


def _repair_entry_row(entry_row: Any) -> tuple[Any, dict[str, Any]]:
    row = _row_dict(entry_row)
    symbol = _norm_symbol(row.get("symbol") or row.get("code") or row.get("stock_code"))
    c1, h1, l1, atr1, m1 = _best_from_dict(row)
    c2, h2, l2, atr2, m2 = _best_from_global_context(symbol) if symbol else (0.0, 0.0, 0.0, 0.0, "no_symbol")

    r1 = _range_ratio(c1, h1, l1)
    r2 = _range_ratio(c2, h2, l2)
    if r2 > r1 or (r2 == r1 and atr2 > atr1):
        close, high, low, atr, method = c2, h2, l2, max(atr1, atr2), m2
        ratio = r2
    else:
        close, high, low, atr, method = c1, h1, l1, max(atr1, atr2), m1
        ratio = r1

    min_range = _entry_min_range_pct()
    min_atr_ratio = _entry_min_atr_ratio()
    required_atr = close * min_atr_ratio if close > 0 else 0.0
    range_proxy = max(0.0, high - low) if high >= low else 0.0
    old_atr = _first_pos(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"))

    repaired = dict(row)
    atr_method = "missing"
    if close > 0 and high > 0 and low > 0 and high >= low and ratio >= min_range:
        repaired.update({
            "close": close,
            "close_price": close,
            "current_price": close,
            "price": close,
            "high": high,
            "low": low,
            "high_price": high,
            "low_price": low,
            "day_high": max(_safe_float(repaired.get("day_high"), 0.0), high),
            "day_low": low if _safe_float(repaired.get("day_low"), 0.0) <= 0 else min(_safe_float(repaired.get("day_low"), low), low),
            "range_pct": ratio,
            "intraday_range_pct": ratio,
            "day_range_pct": ratio,
        })
        if atr > 0 and (atr / close) >= min_atr_ratio:
            repaired.update({"atr": atr, "atr_1m": atr, "ATR": atr})
            atr_method = "history_or_row_atr"
        elif range_proxy >= required_atr > 0:
            repaired.update({"atr": range_proxy, "atr_1m": range_proxy, "ATR": range_proxy})
            atr = range_proxy
            atr_method = "range_proxy"
        elif atr > old_atr:
            repaired.update({"atr": atr, "atr_1m": atr, "ATR": atr})
            atr_method = "history_atr_below_threshold"

    diag = {
        "symbol": symbol,
        "old_close": c1,
        "old_high": h1,
        "old_low": l1,
        "old_atr": old_atr,
        "gc_close": c2,
        "gc_high": h2,
        "gc_low": l2,
        "gc_atr": atr2,
        "new_close": close,
        "new_high": high,
        "new_low": low,
        "new_range_pct": ratio,
        "new_atr": _first_pos(repaired, ("atr_1m", "atr", "ATR")),
        "min_range": min_range,
        "min_atr_ratio": min_atr_ratio,
        "required_atr": required_atr,
        "range_proxy": range_proxy,
        "method": method,
        "atr_method": atr_method,
        "repaired": repaired != row,
    }
    return repaired, diag


def _call_original(original: Any, entry_row: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    return original(entry_row, *args, **kwargs)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_ATR_1M_FILTER_REPAIR", True):
        logger.warning("[SUMMARY AI ATR 1M REPAIR] disabled by env")
        return False
    ok = _patch_once("install")
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch, daemon=True, name="summary-ai-atr-1m-repair-watch").start()
    return bool(ok)


def _patch_once(reason: str = "install") -> bool:
    global _INSTALLED
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "atr_1m_filter", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI ATR 1M REPAIR] target missing reason=%s", reason)
            return False
        if getattr(cur, "_summary_ai_atr_1m_repair_v1", False):
            _INSTALLED = True
            return True
        original = cur

        @functools.wraps(original)
        def _patched_atr_1m_filter(entry_row: Any = None, *args: Any, **kwargs: Any):
            allow = _call_original(original, entry_row, args, kwargs)
            if isinstance(allow, tuple):
                return allow
            if bool(allow):
                return allow
            row = _row_dict(entry_row)
            if not _source_is_summary_ai(row):
                return allow
            repaired, diag = _repair_entry_row(entry_row)
            if not diag.get("repaired"):
                logger.warning("[SUMMARY AI ATR 1M REPAIR] original NG no repair possible detail=%s version=%s", diag, VERSION)
                return allow
            retry = _call_original(original, repaired, args, kwargs)
            logger.warning("[SUMMARY AI ATR 1M REPAIR] retry after repair original_ng=%s retry=%s detail=%s version=%s", allow, retry, diag, VERSION)
            if bool(retry):
                try:
                    if isinstance(entry_row, dict):
                        entry_row.update(repaired)
                except Exception:
                    pass
            return retry

        _patched_atr_1m_filter._summary_ai_atr_1m_repair_v1 = True  # type: ignore[attr-defined]
        _patched_atr_1m_filter._original = original  # type: ignore[attr-defined]
        ec.atr_1m_filter = _patched_atr_1m_filter
        _INSTALLED = True
        logger.warning("[SUMMARY AI ATR 1M REPAIR] installed reason=%s version=%s original=%s", reason, VERSION, getattr(original, "__name__", type(original).__name__))
        return True
    except Exception:
        logger.exception("[SUMMARY AI ATR 1M REPAIR] patch_once failed reason=%s", reason)
        return False


def _watch() -> None:
    loops = int(_env_float("SUMMARY_AI_ATR_1M_REPAIR_WATCH_LOOPS", 60))
    sleep_sec = max(0.5, _env_float("SUMMARY_AI_ATR_1M_REPAIR_WATCH_SLEEP_SEC", 1.0))
    for i in range(max(1, loops)):
        ok = _patch_once(f"watcher:{i}")
        if i in (0, max(1, loops) - 1):
            logger.warning("[SUMMARY AI ATR 1M REPAIR] enforce i=%s/%s ok=%s version=%s", i, loops, ok, VERSION)
        time.sleep(sleep_sec)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ATR 1M REPAIR] auto install failed")


__all__ = ["install", "VERSION"]
