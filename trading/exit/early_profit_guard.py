from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_STATE: dict[str, dict[str, Any]] = {}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return float(default)
        return x
    except Exception:
        return float(default)


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(obj, dict) and name in obj:
                return obj.get(name)
            if hasattr(obj, name):
                return getattr(obj, name)
        except Exception:
            pass
    return default


def _side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "BUY_CREDIT", "LONG", "L", "2", "02", "20", "B", "信用買", "買", "買建", "買い", "新規買"}:
        return "BUY"
    if s in {"SELL", "SELL_CREDIT", "SHORT", "S", "1", "01", "10", "信用売", "売", "売建", "売り", "新規売"}:
        return "SELL"
    return s


def _parse_time(v: Any) -> Optional[dt.datetime]:
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    try:
        s = str(v or "").strip()
        if not s:
            return None
        s = s.replace("T", " ").split("+", 1)[0].rstrip("Z")
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _hold_seconds(pos: dict[str, Any], ctx: Any, now: dt.datetime) -> float:
    t = _parse_time(_get(ctx, "entry_time", default=None) or _get(pos, "entry_time", "created_at", "timestamp", default=None))
    if t is None:
        return 0.0
    try:
        return max(0.0, (now - t).total_seconds())
    except Exception:
        return 0.0


def _key(symbol: str, side: str, entry_price: float, pos: dict[str, Any], ctx: Any) -> str:
    t = _get(ctx, "entry_time", default=None) or _get(pos, "entry_time", "created_at", "timestamp", default="")
    return f"{symbol}|{side}|{entry_price:.6f}|{t}"


def _extract_high_low(ctx: Any, bar5s: Any, entry_price: float, current_price: float) -> tuple[float, float]:
    highs = [entry_price, current_price]
    lows = [entry_price, current_price]

    for obj in (ctx, bar5s):
        for name in ("high_after_entry", "highest_price", "max_price", "high", "High", "h", "H"):
            x = _sf(_get(obj, name, default=None), 0.0)
            if x > 0:
                highs.append(x)
        for name in ("low_after_entry", "lowest_price", "min_price", "low", "Low", "l", "L"):
            x = _sf(_get(obj, name, default=None), 0.0)
            if x > 0:
                lows.append(x)

    return max(highs), min(lows)


def _tracked(symbol: str, side: str, entry_price: float, current_price: float, pos: dict[str, Any], ctx: Any, now: dt.datetime, bar5s: Any) -> tuple[float, float]:
    high0, low0 = _extract_high_low(ctx, bar5s, entry_price, current_price)
    key = _key(symbol, side, entry_price, pos, ctx)
    st = _STATE.get(key)
    if not st:
        st = {"high": high0, "low": low0, "updated_at": now}
        _STATE[key] = st
        logger.warning("[EARLY PROFIT GUARD] tracking start symbol=%s side=%s entry=%.4f price=%.4f high=%.4f low=%.4f", symbol, side, entry_price, current_price, high0, low0)
    else:
        st["high"] = max(_sf(st.get("high"), high0), high0)
        st["low"] = min(_sf(st.get("low"), low0), low0)
        st["updated_at"] = now

    try:
        setattr(ctx, "high_after_entry", float(st["high"]))
        setattr(ctx, "low_after_entry", float(st["low"]))
    except Exception:
        pass

    return float(st["high"]), float(st["low"])


def judge_early_profit_guard(*, symbol: str, pos: dict[str, Any], side: str, entry_price: float, current_price: float, ctx: Any, now: dt.datetime, bar5s: Any = None) -> Tuple[bool, str]:
    if not _env_bool("EARLY_PROFIT_GUARD_ENABLED", True):
        return False, ""

    side = _side(side)
    entry_price = _sf(entry_price)
    current_price = _sf(current_price)
    if side not in {"BUY", "SELL"} or entry_price <= 0 or current_price <= 0:
        logger.warning("[EARLY PROFIT GUARD] skip invalid symbol=%s side=%s entry=%.4f price=%.4f", symbol, side, entry_price, current_price)
        return False, ""

    threshold = _env_float("TRAILING_DRAWDOWN_PCT", 0.0030)
    take_profit = _env_float("TAKE_PROFIT_PCT", _env_float("EARLY_TAKE_PROFIT_PCT", 0.0))
    no_progress_sec = _env_float("EARLY_NO_PROGRESS_SECONDS", 15.0)
    no_progress_need = _env_float("EARLY_NO_PROGRESS_NEED_PCT", 0.0005)

    hold = _hold_seconds(pos, ctx, now)
    high, low = _tracked(symbol, side, entry_price, current_price, pos, ctx, now, bar5s)

    if side == "BUY":
        profit = (current_price - entry_price) / entry_price
        adverse_from_entry = (entry_price - current_price) / entry_price
        adverse_from_extreme = (high - current_price) / high if high > 0 else 0.0
        max_profit = (high - entry_price) / entry_price
    else:
        profit = (entry_price - current_price) / entry_price
        adverse_from_entry = (current_price - entry_price) / entry_price
        adverse_from_extreme = (current_price - low) / low if low > 0 else 0.0
        max_profit = (entry_price - low) / entry_price

    logger.warning(
        "[EARLY PROFIT GUARD] check symbol=%s side=%s hold=%.1fs entry=%.4f price=%.4f high=%.4f low=%.4f profit=%.4f%% entry_adverse=%.4f%% extreme_adverse=%.4f%% threshold=%.4f%%",
        symbol, side, hold, entry_price, current_price, high, low, profit * 100.0, adverse_from_entry * 100.0, adverse_from_extreme * 100.0, threshold * 100.0,
    )

    if threshold > 0 and adverse_from_entry >= threshold:
        reason = f"ENTRY_ADVERSE_EXIT_{side}"
        logger.warning("[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s", symbol, reason)
        return True, reason

    if threshold > 0 and adverse_from_extreme >= threshold:
        reason = f"TRAILING_DRAWDOWN_{side}"
        logger.warning("[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s", symbol, reason)
        return True, reason

    if take_profit > 0 and profit >= take_profit:
        reason = f"TAKE_PROFIT_{side}"
        logger.warning("[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s", symbol, reason)
        return True, reason

    if hold >= no_progress_sec and max_profit < no_progress_need:
        reason = f"EARLY_NO_PROGRESS_{side}"
        logger.warning("[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s", symbol, reason)
        return True, reason

    return False, ""


__all__ = ["judge_early_profit_guard"]
