# -*- coding: utf-8 -*-
"""Keep normal stop-loss quick and protect profits after they appear.

User policy:
- Stop loss should remain quick: -0.30%.
- Take-profit side keeps the previous normal behavior:
  +0.80% fixed take profit, +0.60% trailing start, -0.30% trailing gap,
  and 300 seconds max hold.
- Once a position has been profitable, avoid letting it fall back to a loss:
  +0.20% max profit -> breakeven/profit-tick floor
  +0.40% max profit -> +0.10% floor
  +0.60% max profit -> +0.25% floor

This keeps the existing exit pipeline and wraps only check_normal_exit.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-NORMAL-PROFIT-FLOOR-PROTECTION"
_INSTALLED = False
_ORIGINAL_CHECK_NORMAL_EXIT = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _set(obj: Any, name: str, value: Any) -> None:
    try:
        if isinstance(obj, dict):
            obj[name] = value
        else:
            setattr(obj, name, value)
    except Exception:
        pass


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_sell_side(pos: Any) -> bool:
    try:
        side = str(_get(pos, "side") or _get(pos, "Side") or "").upper()
        return side.startswith("SELL") or side.startswith("SHORT")
    except Exception:
        return False


def _entry_price(pos: Any) -> float:
    return _safe_float(
        _get(pos, "avg_price")
        or _get(pos, "entry_price")
        or _get(pos, "price")
        or _get(pos, "current_price"),
        0.0,
    )


def _pnl_rate(pos: Any, price: float) -> float:
    entry = _entry_price(pos)
    if entry <= 0 or price <= 0:
        return 0.0
    rate = (float(price) - entry) / entry
    if _is_sell_side(pos):
        rate = -rate
    return rate


def _patched_check_normal_exit(pos: Any, price: float, now: dt.datetime):
    try:
        pnl_rate = _pnl_rate(pos, float(price or 0.0))
        max_profit_rate = max(_safe_float(_get(pos, "max_profit_rate"), 0.0), pnl_rate)
        _set(pos, "max_profit_rate", max_profit_rate)

        # Profit floor protection. Values are configurable, but default to the
        # requested policy. Small +0.05% breakeven buffer helps avoid fees/slip.
        breakeven_trigger = abs(_env_float("NORMAL_PROFIT_FLOOR_BREAKEVEN_TRIGGER_PCT", 0.0020))
        breakeven_floor = _env_float("NORMAL_PROFIT_FLOOR_BREAKEVEN_PCT", 0.0005)
        floor1_trigger = abs(_env_float("NORMAL_PROFIT_FLOOR_1_TRIGGER_PCT", 0.0040))
        floor1_rate = _env_float("NORMAL_PROFIT_FLOOR_1_PCT", 0.0010)
        floor2_trigger = abs(_env_float("NORMAL_PROFIT_FLOOR_2_TRIGGER_PCT", 0.0060))
        floor2_rate = _env_float("NORMAL_PROFIT_FLOOR_2_PCT", 0.0025)

        if max_profit_rate >= floor2_trigger and pnl_rate <= floor2_rate:
            return "NORMAL_PROFIT_FLOOR_025"
        if max_profit_rate >= floor1_trigger and pnl_rate <= floor1_rate:
            return "NORMAL_PROFIT_FLOOR_010"
        if max_profit_rate >= breakeven_trigger and pnl_rate <= breakeven_floor:
            return "NORMAL_BREAKEVEN_STOP"
    except Exception:
        logger.exception("[NORMAL EXIT PROFIT FLOOR] precheck failed; fallback original")

    try:
        if callable(_ORIGINAL_CHECK_NORMAL_EXIT):
            return _ORIGINAL_CHECK_NORMAL_EXIT(pos, price, now)
    except Exception:
        logger.exception("[NORMAL EXIT PROFIT FLOOR] original check failed")
    return None


def install() -> bool:
    global _INSTALLED, _ORIGINAL_CHECK_NORMAL_EXIT
    if _INSTALLED:
        return True
    if os.environ.get("DISABLE_NORMAL_EXIT_SCALP_ALIGN_PATCH", "").strip() == "1":
        logger.warning("[NORMAL EXIT SCALP ALIGN] disabled by env")
        return False
    try:
        import trading.handlers.exit_handler as eh

        old = {
            "NORMAL_STOP_LOSS": getattr(eh, "NORMAL_STOP_LOSS", None),
            "NORMAL_TAKE_PROFIT": getattr(eh, "NORMAL_TAKE_PROFIT", None),
            "NORMAL_TRAIL_START": getattr(eh, "NORMAL_TRAIL_START", None),
            "NORMAL_TRAIL_GAP": getattr(eh, "NORMAL_TRAIL_GAP", None),
            "NORMAL_MAX_HOLD_SEC": getattr(eh, "NORMAL_MAX_HOLD_SEC", None),
        }

        # Keep the requested quick stop-loss, but restore the normal profit side.
        stop = -abs(_env_float("NORMAL_EXIT_STOP_LOSS_PCT", 0.0030))
        take = abs(_env_float("NORMAL_EXIT_TAKE_PROFIT_PCT", 0.0080))
        trail_start = abs(_env_float("NORMAL_EXIT_TRAIL_START_PCT", 0.0060))
        trail_gap = abs(_env_float("NORMAL_EXIT_TRAIL_GAP_PCT", 0.0030))
        max_hold = int(_env_float("NORMAL_EXIT_MAX_HOLD_SEC", 300.0))

        eh.NORMAL_STOP_LOSS = stop
        eh.NORMAL_TAKE_PROFIT = take
        eh.NORMAL_TRAIL_START = trail_start
        eh.NORMAL_TRAIL_GAP = trail_gap
        eh.NORMAL_MAX_HOLD_SEC = max_hold

        current = getattr(eh, "check_normal_exit", None)
        if current is not _patched_check_normal_exit:
            _ORIGINAL_CHECK_NORMAL_EXIT = current
            eh.check_normal_exit = _patched_check_normal_exit

        _INSTALLED = True
        logger.warning(
            "[NORMAL EXIT PROFIT FLOOR] installed version=%s stop %.4f->%.4f take %.4f->%.4f trail_start %.4f->%.4f trail_gap %.4f->%.4f max_hold %s->%s floors=0.20%%->0.05%%,0.40%%->0.10%%,0.60%%->0.25%% policy=profit_original_plus_floor",
            VERSION,
            old["NORMAL_STOP_LOSS"] if old["NORMAL_STOP_LOSS"] is not None else 0.0,
            eh.NORMAL_STOP_LOSS,
            old["NORMAL_TAKE_PROFIT"] if old["NORMAL_TAKE_PROFIT"] is not None else 0.0,
            eh.NORMAL_TAKE_PROFIT,
            old["NORMAL_TRAIL_START"] if old["NORMAL_TRAIL_START"] is not None else 0.0,
            eh.NORMAL_TRAIL_START,
            old["NORMAL_TRAIL_GAP"] if old["NORMAL_TRAIL_GAP"] is not None else 0.0,
            eh.NORMAL_TRAIL_GAP,
            old["NORMAL_MAX_HOLD_SEC"],
            eh.NORMAL_MAX_HOLD_SEC,
        )
        return True
    except Exception:
        logger.exception("[NORMAL EXIT PROFIT FLOOR] install failed version=%s", VERSION)
        return False


__all__ = ["VERSION", "install"]
