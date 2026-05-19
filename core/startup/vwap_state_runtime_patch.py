# ============================================================
# File   : core/startup/vwap_state_runtime_patch.py
# Version: Ver01-PATCH-DIRECTION-STRENGTH-VWAP-STATE
# ------------------------------------------------------------
# entry_direction_confirm_guard_patch._calc_direction_strength を包み、
# VWAP上/下の継続状態を方向強度へ加算する。
#
# 目的:
#   - VWAP上抜け/下抜けの一瞬だけではなく、
#     その後 close > vwap / close < vwap が継続している状態を評価する
#   - VWAP乖離拡大は順方向、乖離縮小/乖離過大は伸び切り警戒にする
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_CALC_DIRECTION_STRENGTH = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _patched_calc_direction_strength(row: dict):
    if not callable(_ORIG_CALC_DIRECTION_STRENGTH):
        return 0.0, ["vwap_patch_original_missing"]

    strength, reasons = _ORIG_CALC_DIRECTION_STRENGTH(row)
    try:
        strength = _safe_float(strength, 0.0)
        reasons = list(reasons or [])
    except Exception:
        strength = 0.0
        reasons = []

    if not _env_bool("ENTRY_VWAP_STATE_ENABLED", True):
        return strength, reasons

    try:
        from trading.entry.vwap_state_detector import detect_vwap_state
        res = detect_vwap_state(row)
        delta = _safe_float(res.get("score_delta"), 0.0)
        state = str(res.get("state") or "")
        vwap_reasons = res.get("reasons") or []
        if delta != 0.0:
            strength += delta
            reasons.append(f"vwap_state:{state}:{delta:+.2f}")
            for r in vwap_reasons[:5]:
                reasons.append(f"vwap_{r}")
        logger.info(
            "[VWAP STATE PATCH] state=%s delta=%.3f strength_after=%.3f reasons=%s",
            state,
            delta,
            strength,
            vwap_reasons,
        )
    except Exception:
        logger.exception("[VWAP STATE PATCH] detect/apply failed")
        reasons.append("vwap_state_error")

    return float(strength), reasons


def install() -> bool:
    global _INSTALLED, _ORIG_CALC_DIRECTION_STRENGTH
    try:
        import core.startup.entry_direction_confirm_guard_patch as p

        current = getattr(p, "_calc_direction_strength", None)
        if getattr(current, "_vwap_state_runtime_patch", False):
            _INSTALLED = True
            return True
        if not callable(current):
            logger.error("[VWAP STATE PATCH] target _calc_direction_strength unavailable")
            return False

        _ORIG_CALC_DIRECTION_STRENGTH = current
        _patched_calc_direction_strength._vwap_state_runtime_patch = True  # type: ignore[attr-defined]
        p._calc_direction_strength = _patched_calc_direction_strength
        _INSTALLED = True
        logger.warning(
            "[VWAP STATE PATCH] installed enabled=%s",
            _env_bool("ENTRY_VWAP_STATE_ENABLED", True),
        )
        return True
    except Exception:
        logger.exception("[VWAP STATE PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[VWAP STATE PATCH] auto install failed")


__all__ = ["install"]
