# -*- coding: utf-8 -*-
"""Align normal EXIT thresholds with Tonosama/Inago scalping policy.

This keeps the existing exit pipeline and only changes NORMAL_* constants.
Normal positions still use check_normal_exit, but the thresholds become:
- stop loss: -0.30%
- take profit trigger: +0.20%
- trailing start: +0.20%
- trailing gap: -0.20% from max profit
- max hold: 60 seconds
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
VERSION = "V1-NORMAL-EXIT-SCALP-ALIGN-TONOSAMA"
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

        stop = -abs(_env_float("NORMAL_EXIT_STOP_LOSS_PCT", _env_float("TONOSAMA_EXIT_STOP_LOSS_PCT", 0.0030)))
        take = abs(_env_float("NORMAL_EXIT_TAKE_PROFIT_PCT", _env_float("TONOSAMA_EXIT_TAKE_PROFIT_PCT", 0.0020)))
        trail_gap = abs(_env_float("NORMAL_EXIT_TRAIL_GAP_PCT", _env_float("TONOSAMA_EXIT_TRAIL_GAP_PCT", 0.0020)))
        trail_start = abs(_env_float("NORMAL_EXIT_TRAIL_START_PCT", take))
        max_hold = int(_env_float("NORMAL_EXIT_MAX_HOLD_SEC", _env_float("TONOSAMA_EXIT_MAX_HOLD_SEC", 60.0)))

        eh.NORMAL_STOP_LOSS = stop
        eh.NORMAL_TAKE_PROFIT = take
        eh.NORMAL_TRAIL_START = trail_start
        eh.NORMAL_TRAIL_GAP = trail_gap
        eh.NORMAL_MAX_HOLD_SEC = max_hold

        _INSTALLED = True
        logger.warning(
            "[NORMAL EXIT SCALP ALIGN] installed version=%s stop %.4f->%.4f take %.4f->%.4f trail_start %.4f->%.4f trail_gap %.4f->%.4f max_hold %s->%s",
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
