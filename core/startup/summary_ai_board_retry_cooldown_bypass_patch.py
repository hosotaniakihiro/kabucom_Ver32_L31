# -*- coding: utf-8 -*-
"""
SUMMARY_AI board retry cooldown bypass for push-rotation wait.

The normal board retry layer correctly sets a long REST cooldown after API/rate
or register related errors. However, SUMMARY_AI order building waits about two
PUSH rotations before deciding STRICT_BOARD_MISSING. During that wait window the
long cooldown can make every retry a COOLDOWN_SKIP, so the 10.5 second wait does
not actually re-check the board.

This patch keeps board-missing as a hard block, but allows SUMMARY_AI board
checks to make a controlled REST attempt during the rotation wait. Calls are
rate-limited per symbol, so we do not hammer kabu Station.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-BOARD-COOLDOWN-BYPASS-RATE-LIMITED"
_INSTALLED = False
_LAST_TRY: dict[str, float] = {}
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _is_summary_ai(source: Any) -> bool:
    s = str(source or "").strip().upper()
    return s in {"SUMMARY_AI", "SUMMARY", "PUSH_SUMMARY", "STOCK_SUMMARY"} or "SUMMARY_AI" in s


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_BOARD_COOLDOWN_BYPASS_ENABLED", True):
        logger.warning("[SUMMARY AI BOARD COOLDOWN BYPASS] disabled version=%s", VERSION)
        return False
    try:
        import core.startup.board_retry_patch as brp
        original = getattr(brp, "_fetch_board_rest", None)
        if not callable(original):
            logger.warning("[SUMMARY AI BOARD COOLDOWN BYPASS] target missing version=%s", VERSION)
            return False
        if getattr(original, "_summary_ai_board_cooldown_bypass_v1", False):
            _INSTALLED = True
            return True

        def _patched_fetch_board_rest(symbol: str, side: str = "", source: str = ""):
            sym = _norm_symbol(symbol)
            now = time.time()
            min_interval = max(0.5, _env_float("SUMMARY_AI_BOARD_REST_MIN_INTERVAL_SEC", 1.0))
            cooldown_until = float(getattr(brp, "_REST_COOLDOWN_UNTIL", 0.0) or 0.0)
            if _is_summary_ai(source) and cooldown_until > now:
                last = float(_LAST_TRY.get(sym, 0.0) or 0.0)
                if now - last < min_interval:
                    logger.warning(
                        "[SUMMARY AI BOARD COOLDOWN BYPASS] per-symbol throttle symbol=%s side=%s source=%s wait=%.2fs cooldown_remaining=%.1fs version=%s",
                        sym, side, source, max(0.0, min_interval - (now - last)), cooldown_until - now, VERSION,
                    )
                    return None
                _LAST_TRY[sym] = now
                old_until = cooldown_until
                try:
                    setattr(brp, "_REST_COOLDOWN_UNTIL", 0.0)
                    logger.warning(
                        "[SUMMARY AI BOARD COOLDOWN BYPASS] temporary bypass symbol=%s side=%s source=%s old_remaining=%.1fs min_interval=%.2fs version=%s",
                        sym, side, source, old_until - now, min_interval, VERSION,
                    )
                    return original(symbol, side=side, source=source)
                finally:
                    # Preserve any new cooldown created by the REST call; otherwise restore
                    # the previous cooldown for non-SUMMARY callers.
                    new_until = float(getattr(brp, "_REST_COOLDOWN_UNTIL", 0.0) or 0.0)
                    if new_until <= time.time() and old_until > time.time():
                        setattr(brp, "_REST_COOLDOWN_UNTIL", old_until)
            return original(symbol, side=side, source=source)

        _patched_fetch_board_rest._summary_ai_board_cooldown_bypass_v1 = True  # type: ignore[attr-defined]
        _patched_fetch_board_rest._original = original  # type: ignore[attr-defined]
        brp._fetch_board_rest = _patched_fetch_board_rest
        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI BOARD COOLDOWN BYPASS] installed version=%s min_interval=%s hard_block_remains=True",
            VERSION, os.getenv("SUMMARY_AI_BOARD_REST_MIN_INTERVAL_SEC", "1.0"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI BOARD COOLDOWN BYPASS] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BOARD COOLDOWN BYPASS] auto install failed version=%s", VERSION)


__all__ = ["install", "VERSION"]
