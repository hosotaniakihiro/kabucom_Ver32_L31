# -*- coding: utf-8 -*-
from __future__ import annotations

import functools
import logging
import math
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-SUMMARY-AI-ATR-LIQUIDITY-BRIDGE"
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
        cond = d.get("entry_conditions")
        if isinstance(cond, dict):
            for k, v in cond.items():
                if k not in d or d.get(k) in (None, ""):
                    d[k] = v
        return d
    except Exception:
        return {}


def _first_pos(d: dict[str, Any], keys: tuple[str, ...]) -> float:
    for k in keys:
        try:
            x = _safe_float(d.get(k), 0.0)
            if x > 0:
                return x
        except Exception:
            pass
    return 0.0


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
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


def _best_price_range(row: dict[str, Any]) -> tuple[float, float, float, float, str]:
    close = _first_pos(row, ("close", "close_price", "current_price", "price", "last_price", "entry_price", "limit_price"))
    atr = _first_pos(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"))
    best = (close, 0.0, 0.0, atr, "missing")
    best_ratio = 0.0
    pairs = [
        (("high", "high_price", "bar_high", "summary_high"), ("low", "low_price", "bar_low", "summary_low"), "row_high_low"),
        (("day_high", "today_high", "intraday_high", "session_high", "range_high", "ranking_high", "snapshot_high", "high_price_day", "ai_disp_day_high", "ai_disp_high"),
         ("day_low", "today_low", "intraday_low", "session_low", "range_low", "ranking_low", "snapshot_low", "low_price_day", "ai_disp_day_low", "ai_disp_low"), "row_day_high_low"),
        (("high_1m_max", "recent_high", "recent_1m_high", "high_3m", "high_5m"), ("low_1m_min", "recent_low", "recent_1m_low", "low_3m", "low_5m"), "recent_or_mtf_high_low"),
    ]
    for hk, lk, label in pairs:
        h = _first_pos(row, hk)
        l = _first_pos(row, lk)
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
        s = df[df["symbol"].astype(str).str.replace(r"\.0$", "", regex=True) == str(symbol)]
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


def _best_from_global_context(symbol: str) -> tuple[float, float, float, float, str, dict[str, Any]]:
    try:
        from core.global_context.context import global_context
    except Exception:
        try:
            from core.global_context import global_context  # type: ignore
        except Exception:
            return 0.0, 0.0, 0.0, 0.0, "gc_import_failed", {}

    best = (0.0, 0.0, 0.0, 0.0, "gc_missing", {})
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
            c, h, l, atr, method = _best_price_range(d)
            ratio = _range_ratio(c, h, l)
            if ratio > best_ratio or (ratio == best_ratio and atr > best[3]):
                best = (c, h, l, atr, f"{label}:{method}", d)
                best_ratio = ratio
        except Exception:
            logger.debug("[SUMMARY AI ATR LIQ BRIDGE] global lookup failed getter=%s symbol=%s", getter_name, symbol, exc_info=True)
    return best


def _liquidity_values(row: dict[str, Any], gc_row: dict[str, Any]) -> tuple[float, float, float]:
    merged = dict(gc_row or {})
    merged.update({k: v for k, v in row.items() if v not in (None, "")})
    latest_vol = _first_pos(merged, ("latest_volume", "display_volume", "volume", "vol", "_latest_volume", "bar_volume", "volume_1m"))
    avg_vol = _first_pos(merged, ("avg_volume_5", "recent_avg_volume", "avg_vol_5", "volume_avg5", "vol_avg5", "avg_volume"))
    turnover = _first_pos(merged, ("turnover", "trading_value", "sales_value", "display_turnover", "turnover_1m", "recent_turnover"))
    price = _first_pos(merged, ("close", "close_price", "current_price", "price", "last_price"))
    if turnover <= 0 and latest_vol > 0 and price > 0:
        turnover = latest_vol * price
    if avg_vol <= 0:
        avg_vol = latest_vol
    return latest_vol, avg_vol, turnover


def _liquidity_ok(row: dict[str, Any], gc_row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    latest_vol, avg_vol, turnover = _liquidity_values(row, gc_row)
    min_latest = _env_float("SUMMARY_AI_ATR_BRIDGE_MIN_LATEST_VOLUME", 3000.0)
    min_avg = _env_float("SUMMARY_AI_ATR_BRIDGE_MIN_AVG_VOLUME", 3000.0)
    min_turnover = _env_float("SUMMARY_AI_ATR_BRIDGE_MIN_TURNOVER", 1000000.0)
    ok = latest_vol >= min_latest and avg_vol >= min_avg and turnover >= min_turnover
    return ok, {
        "latest_vol": latest_vol,
        "avg_vol": avg_vol,
        "turnover": turnover,
        "min_latest": min_latest,
        "min_avg": min_avg,
        "min_turnover": min_turnover,
    }


def _build_repaired(entry_row: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    row = _row_dict(entry_row)
    symbol = _norm_symbol(row.get("symbol") or row.get("code") or row.get("stock_code"))
    c1, h1, l1, atr1, m1 = _best_price_range(row)
    c2, h2, l2, atr2, m2, gc_row = _best_from_global_context(symbol) if symbol else (0.0, 0.0, 0.0, 0.0, "no_symbol", {})
    r1 = _range_ratio(c1, h1, l1)
    r2 = _range_ratio(c2, h2, l2)
    if r2 > r1 or (r2 == r1 and atr2 > atr1):
        close, high, low, atr, method, ratio = c2, h2, l2, max(atr1, atr2), m2, r2
    else:
        close, high, low, atr, method, ratio = c1, h1, l1, max(atr1, atr2), m1, r1

    min_atr_ratio = _entry_min_atr_ratio()
    required_atr = close * min_atr_ratio if close > 0 else 0.0
    range_proxy = max(0.0, high - low) if high >= low else 0.0
    old_atr = _first_pos(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"))
    min_actual_range = _env_float("SUMMARY_AI_ATR_BRIDGE_MIN_ACTUAL_RANGE_PCT", 0.0010)
    liq_ok, liq_diag = _liquidity_ok(row, gc_row)

    repaired = dict(row)
    bridge_ok = False
    atr_method = "missing"
    if close > 0 and liq_ok and ratio >= min_actual_range:
        # Keep the original ATR guard threshold. We only synthesize ATR for SUMMARY_AI rows
        # when liquidity and actual observed range prove that the symbol is tradable.
        synthetic_atr = max(old_atr, atr, range_proxy, required_atr)
        high2 = high if high > 0 else close + synthetic_atr / 2.0
        low2 = low if low > 0 else max(0.01, close - synthetic_atr / 2.0)
        repaired.update({
            "close": close,
            "close_price": close,
            "current_price": close,
            "price": close,
            "high": high2,
            "low": low2,
            "high_price": high2,
            "low_price": low2,
            "day_high": max(_safe_float(repaired.get("day_high"), 0.0), high2),
            "day_low": low2 if _safe_float(repaired.get("day_low"), 0.0) <= 0 else min(_safe_float(repaired.get("day_low"), low2), low2),
            "range_pct": max(ratio, _range_ratio(close, high2, low2)),
            "intraday_range_pct": max(ratio, _range_ratio(close, high2, low2)),
            "day_range_pct": max(ratio, _range_ratio(close, high2, low2)),
            "atr": synthetic_atr,
            "atr_1m": synthetic_atr,
            "ATR": synthetic_atr,
            "summary_ai_atr_bridge": True,
        })
        bridge_ok = True
        atr_method = "liquidity_gated_bridge"

    diag = {
        "symbol": symbol,
        "old_atr": old_atr,
        "close": close,
        "high": high,
        "low": low,
        "range_pct": ratio,
        "min_actual_range": min_actual_range,
        "min_atr_ratio": min_atr_ratio,
        "required_atr": required_atr,
        "range_proxy": range_proxy,
        "method": method,
        "atr_method": atr_method,
        "liquidity_ok": liq_ok,
        **liq_diag,
        "bridge_ok": bridge_ok,
        "new_atr": _first_pos(repaired, ("atr_1m", "atr", "ATR")),
    }
    return repaired, diag


def _call_original(original: Any, entry_row: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    return original(entry_row, *args, **kwargs)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_ATR_LIQUIDITY_BRIDGE", True):
        logger.warning("[SUMMARY AI ATR LIQ BRIDGE] disabled by env")
        return False
    ok = _patch_once("install")
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch, daemon=True, name="summary-ai-atr-liq-bridge-watch").start()
    return bool(ok)


def _patch_once(reason: str = "install") -> bool:
    global _INSTALLED
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "atr_1m_filter", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI ATR LIQ BRIDGE] target missing reason=%s", reason)
            return False
        if getattr(cur, "_summary_ai_atr_liq_bridge_v1", False):
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
            repaired, diag = _build_repaired(entry_row)
            if not diag.get("bridge_ok"):
                logger.warning("[SUMMARY AI ATR LIQ BRIDGE] original NG bridge not allowed detail=%s version=%s", diag, VERSION)
                return allow
            retry = _call_original(original, repaired, args, kwargs)
            logger.warning("[SUMMARY AI ATR LIQ BRIDGE] retry after liquidity bridge original_ng=%s retry=%s detail=%s version=%s", allow, retry, diag, VERSION)
            if bool(retry):
                try:
                    if isinstance(entry_row, dict):
                        entry_row.update(repaired)
                except Exception:
                    pass
            return retry

        _patched_atr_1m_filter._summary_ai_atr_liq_bridge_v1 = True  # type: ignore[attr-defined]
        _patched_atr_1m_filter._original = original  # type: ignore[attr-defined]
        ec.atr_1m_filter = _patched_atr_1m_filter
        _INSTALLED = True
        logger.warning("[SUMMARY AI ATR LIQ BRIDGE] installed reason=%s version=%s original=%s", reason, VERSION, getattr(original, "__name__", type(original).__name__))
        return True
    except Exception:
        logger.exception("[SUMMARY AI ATR LIQ BRIDGE] patch_once failed reason=%s", reason)
        return False


def _watch() -> None:
    loops = int(_env_float("SUMMARY_AI_ATR_LIQ_BRIDGE_WATCH_LOOPS", 60))
    sleep_sec = max(0.5, _env_float("SUMMARY_AI_ATR_LIQ_BRIDGE_WATCH_SLEEP_SEC", 1.0))
    for i in range(max(1, loops)):
        ok = _patch_once(f"watcher:{i}")
        if i in (0, max(1, loops) - 1):
            logger.warning("[SUMMARY AI ATR LIQ BRIDGE] enforce i=%s/%s ok=%s version=%s", i, loops, ok, VERSION)
        time.sleep(sleep_sec)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ATR LIQ BRIDGE] auto install failed")


__all__ = ["install", "VERSION"]
