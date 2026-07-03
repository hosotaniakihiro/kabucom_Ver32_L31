# -*- coding: utf-8 -*-
"""Keep normal stop-loss scalped, but restore normal take-profit behavior.

User policy:
- Stop loss should remain quick: -0.30%.
- Take-profit logic should be restored to the previous normal behavior:
  +0.80% fixed take profit, +0.60% trailing start, -0.30% trailing gap,
  and 300 seconds max hold.

This keeps the existing exit pipeline and only changes NORMAL_* constants.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
VERSION = "V2-NORMAL-STOP-SCALP-PROFIT-ORIGINAL"
_INSTALLED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def install() -> bool:
    global _INSTALLED
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

        _INSTALLED = True
        logger.warning(
            "[NORMAL EXIT SCALP ALIGN] installed version=%s stop %.4f->%.4f take %.4f->%.4f trail_start %.4f->%.4f trail_gap %.4f->%.4f max_hold %s->%s policy=profit_original_stop_scalp",
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
        logger.exception("[NORMAL EXIT SCALP ALIGN] install failed version=%s", VERSION)
        return False


__all__ = ["VERSION", "install"]
