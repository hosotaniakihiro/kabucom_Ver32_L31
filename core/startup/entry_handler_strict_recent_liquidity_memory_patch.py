# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-FRESH-MEMORY-SUMMARY-ON-STALE-DB"
_INSTALLED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _parse_dt(v: Any):
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    try:
        if hasattr(v, "to_pydatetime"):
            x = v.to_pydatetime()
            if isinstance(x, dt.datetime):
                return x.replace(tzinfo=None)
    except Exception:
        pass
    try:
        s = str(v).strip()
        if not s or s.lower() in {"nat", "none", "nan"}:
            return None
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _memory_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    sym = _norm_symbol(symbol)
    if not sym:
        return {"ok_read": False, "reason": "symbol_missing", "source": "memory_summary"}
    try:
        import pandas as pd
        from core.global_context import context as gc
    except Exception as e:
        return {"ok_read": False, "reason": "import_failed", "error": str(e), "source": "memory_summary"}

    frames: list[tuple[str, Any]] = []
    for src in ("push", "ranking", "legacy", "push-cache"):
        try:
            df = gc.get_summary_history(tf=1, source=src)
            if df is not None and not getattr(df, "empty", True):
                frames.append((f"history:{src}", df))
        except Exception:
            pass
    for src in ("push", "ranking", "legacy", "push-cache"):
        try:
            df = gc.get_merged_summary(tf=1, source=src)
            if df is not None and not getattr(df, "empty", True):
                frames.append((f"merged:{src}", df))
        except Exception:
            pass
    try:
        df = gc.get_push_df()
        if df is not None and not getattr(df, "empty", True):
            frames.append(("push_df", df))
    except Exception:
        pass

    best: dict[str, Any] | None = None
    for source, df in frames:
        try:
            if "symbol" not in df.columns:
                continue
            x = df.copy()
            x["_symbol_norm"] = x["symbol"].map(_norm_symbol)
            x = x[x["_symbol_norm"] == sym].copy()
            if x.empty:
                continue
            time_col = None
            for c in ("datetime", "end_time", "start_time", "time", "created_at", "updated_at"):
                if c in x.columns:
                    time_col = c
                    break
            if time_col:
                x["_dt"] = pd.to_datetime(x[time_col], errors="coerce").dt.tz_localize(None)
                x = x.sort_values("_dt", ascending=True, na_position="first")
            else:
                x["_dt"] = pd.NaT
            tail = x.tail(max(1, int(bars))).copy()
            vol_col = next((c for c in ("volume", "latest_volume", "display_volume", "vol") if c in tail.columns), None)
            price_col = next((c for c in ("close", "close_price", "price", "current_price") if c in tail.columns), None)
            turnover_col = next((c for c in ("turnover", "trading_value", "display_turnover") if c in tail.columns), None)
            if vol_col is None:
                continue
            vol = pd.to_numeric(tail[vol_col], errors="coerce").fillna(0.0)
            price = pd.to_numeric(tail[price_col], errors="coerce").fillna(0.0) if price_col else pd.Series(0.0, index=tail.index)
            if turnover_col:
                turnover = pd.to_numeric(tail[turnover_col], errors="coerce").fillna(0.0)
            else:
                turnover = price * vol
            if turnover.fillna(0).sum() <= 0 and price.fillna(0).gt(0).any():
                turnover = price * vol
            latest = tail.iloc[-1]
            latest_dt = _parse_dt(latest.get("_dt"))
            rows = int(len(tail))
            out = {
                "ok_read": True,
                "source": source,
                "rows": rows,
                "latest_dt": latest_dt.isoformat(sep=" ") if latest_dt else None,
                "latest_close": _f(latest.get(price_col) if price_col else 0.0),
                "latest_volume": float(vol.iloc[-1]) if len(vol) else 0.0,
                "volume_sum": float(vol.sum()),
                "turnover_sum": float(turnover.sum()),
            }
            if best is None:
                best = out
            else:
                bdt = _parse_dt(best.get("latest_dt"))
                if latest_dt is not None and (bdt is None or latest_dt > bdt):
                    best = out
        except Exception:
            logger.debug("[ENTRY FINAL LIQ MEMORY] source scan failed source=%s symbol=%s", source, sym, exc_info=True)
            continue
    if best is None:
        return {"ok_read": False, "reason": "memory_no_rows", "source": "memory_summary"}
    return best


def _memory_ok(symbol: str, side: str, original_detail: Any) -> tuple[bool, str, dict[str, Any]]:
    bars = max(1, _env_int("ENTRY_HANDLER_RECENT_LIQ_BARS", 5))
    min_latest_volume = _env_float("ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME", _env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0))
    min_avg_volume = _env_float("ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME", _env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0))
    min_turnover = _env_float("ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN", _env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1000000.0))
    max_age_sec = _env_float("ENTRY_HANDLER_STRICT_MAX_AGE_SEC", 180.0)
    values = _memory_liquidity_values(symbol, bars)
    latest_dt = _parse_dt(values.get("latest_dt")) if isinstance(values, dict) else None
    age_sec = (dt.datetime.now() - latest_dt).total_seconds() if latest_dt else None
    rows = int(values.get("rows") or 0) if isinstance(values, dict) else 0
    latest_volume = _f(values.get("latest_volume") if isinstance(values, dict) else 0.0)
    avg_volume = _f(values.get("volume_sum") if isinstance(values, dict) else 0.0) / max(1, rows)
    turnover_sum = _f(values.get("turnover_sum") if isinstance(values, dict) else 0.0)
    detail = {
        "symbol": symbol,
        "side": side,
        "bars": bars,
        "rows": rows,
        "latest_volume": latest_volume,
        "avg_volume": avg_volume,
        "turnover_sum": turnover_sum,
        "latest_dt": values.get("latest_dt") if isinstance(values, dict) else None,
        "age_sec": age_sec,
        "min_latest_volume": min_latest_volume,
        "min_avg_volume": min_avg_volume,
        "min_turnover": min_turnover,
        "max_age_sec": max_age_sec,
        "memory_values": values,
        "original_detail": original_detail,
    }
    if not isinstance(values, dict) or not values.get("ok_read"):
        return False, "STRICT_FINAL_LIQ_MEMORY_READ_NG", detail
    if latest_dt is None or age_sec is None or age_sec > max_age_sec:
        return False, f"STRICT_FINAL_LIQ_MEMORY_STALE:{float(age_sec or 0):.0f}>{max_age_sec:.0f}", detail
    if latest_volume < min_latest_volume:
        return False, f"STRICT_FINAL_LIQ_MEMORY_LATEST_VOLUME_LOW:{latest_volume:.0f}<{min_latest_volume:.0f}", detail
    if avg_volume < min_avg_volume:
        return False, f"STRICT_FINAL_LIQ_MEMORY_AVG_VOLUME_LOW:{avg_volume:.0f}<{min_avg_volume:.0f}", detail
    if turnover_sum < min_turnover:
        return False, f"STRICT_FINAL_LIQ_MEMORY_TURNOVER_LOW:{turnover_sum:.0f}<{min_turnover:.0f}", detail
    logger.warning("[ENTRY FINAL LIQ MEMORY] stale DB bypassed by fresh memory source=%s symbol=%s side=%s age=%.1f latest_vol=%.0f avg_vol=%.0f turnover=%.0f version=%s", values.get("source"), symbol, side, float(age_sec), latest_volume, avg_volume, turnover_sum, VERSION)
    return True, "STRICT_FINAL_LIQ_OK_MEMORY_FRESH", detail


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if os.getenv("DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_MEMORY_PATCH", "").strip() == "1":
        logger.warning("[ENTRY FINAL LIQ MEMORY] disabled by env")
        return False
    try:
        import trading.handlers.entry_handler as eh
        cur = getattr(eh, "_final_recent_liquidity_ok", None)
        if not callable(cur):
            logger.warning("[ENTRY FINAL LIQ MEMORY] target missing")
            return False
        if getattr(cur, "_strict_recent_liq_memory_v1", False):
            _INSTALLED = True
            return True

        @wraps(cur)
        def wrapped(symbol: str, side: str):
            ok, reason, detail = cur(symbol, side)
            try:
                if ok:
                    return ok, reason, detail
                text = str(reason or "")
                if text.startswith("STRICT_FINAL_LIQ_STALE"):
                    mem_ok, mem_reason, mem_detail = _memory_ok(symbol, side, detail)
                    if mem_ok:
                        return True, mem_reason, mem_detail
                    merged = dict(mem_detail)
                    merged["original_reason"] = reason
                    return False, reason, merged
            except Exception:
                logger.exception("[ENTRY FINAL LIQ MEMORY] wrapper failed symbol=%s side=%s", symbol, side)
            return ok, reason, detail

        wrapped._strict_recent_liq_memory_v1 = True  # type: ignore[attr-defined]
        wrapped._original = cur  # type: ignore[attr-defined]
        eh._final_recent_liquidity_ok = wrapped
        _INSTALLED = True
        logger.warning("[ENTRY FINAL LIQ MEMORY] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[ENTRY FINAL LIQ MEMORY] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY FINAL LIQ MEMORY] auto install failed")


__all__ = ["install", "VERSION"]
