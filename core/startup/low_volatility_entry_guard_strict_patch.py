# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/low_volatility_entry_guard_strict_patch.py
# Version: V1-STRICT-RANGE-ATR-LOW-VOL-GUARD
# ------------------------------------------------------------
# Purpose:
#   Strengthen low-volatility entry veto for scalp trading.
#
#   The first low_volatility_entry_guard_patch rejected only when all
#   available movement evidence was below threshold.  That was too weak
#   when rescue/bridge patches injected extra fields such as slope or
#   preapproved AI details.  This patch makes range+ATR the primary
#   movement gate: if both are available and both are below threshold,
#   the entry is rejected regardless of score rescue.
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-STRICT-RANGE-ATR-LOW-VOL-GUARD"
_INSTALLED = False
_ORIGINAL_LOW_VOL_BLOCK = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip().replace(",", ""))
    except Exception:
        return float(default)


def _strict_low_vol_block_factory(base_mod, orig_func):
    def strict_low_vol_block(row: Any):
        if not _env_bool("ENTRY_LOW_VOLATILITY_GUARD_ENABLED", True):
            return False, "", {}

        # Scalp-oriented defaults.  User can override from settings.ini/env.
        min_range_pct = _env_float("ENTRY_MIN_RANGE_PCT", 2.50)
        min_atr_pct = _env_float("ENTRY_MIN_ATR_PCT", 2.50)
        min_change_pct = _env_float("ENTRY_MIN_ABS_CHANGE_PCT", 0.35)

        rng = base_mod._range_pct(row)
        atr = base_mod._atr_pct(row)
        chg = base_mod._change_pct(row)
        slp = base_mod._slope_abs(row)
        symbol = base_mod._norm_symbol(base_mod._pick(row, "symbol", "Symbol"))
        side = base_mod._norm_side(base_mod._pick(row, "side", "ai_side", "entry_decision"))

        # Strong rule: if both real movement measures are available and both
        # are below threshold, reject.  This catches rows like range/ATR around
        # 2% that are still too quiet for the current scalp target.
        if rng is not None and atr is not None and rng < min_range_pct and atr < min_atr_pct:
            detail = {
                "symbol": symbol,
                "side": side,
                "range_pct": rng,
                "atr_pct": atr,
                "change_pct": chg,
                "slope_abs": slp,
                "thresholds": {
                    "min_range_pct": min_range_pct,
                    "min_atr_pct": min_atr_pct,
                    "min_abs_change_pct": min_change_pct,
                },
                "block_rule": "range_and_atr_both_below_threshold",
                "evidence_count": 2 + int(chg is not None) + int(slp is not None),
            }
            return True, "low_volatility_range_atr", detail

        # Secondary rule: if either range or ATR is small and actual change is
        # also tiny, reject.  This catches flat rows when one of range/ATR is
        # missing or distorted.
        movement_pairs = []
        if rng is not None and chg is not None:
            movement_pairs.append(("range_change", rng < min_range_pct and chg < min_change_pct))
        if atr is not None and chg is not None:
            movement_pairs.append(("atr_change", atr < min_atr_pct and chg < min_change_pct))
        for rule_name, should_block in movement_pairs:
            if should_block:
                detail = {
                    "symbol": symbol,
                    "side": side,
                    "range_pct": rng,
                    "atr_pct": atr,
                    "change_pct": chg,
                    "slope_abs": slp,
                    "thresholds": {
                        "min_range_pct": min_range_pct,
                        "min_atr_pct": min_atr_pct,
                        "min_abs_change_pct": min_change_pct,
                    },
                    "block_rule": rule_name,
                    "evidence_count": 2,
                }
                return True, "low_volatility_change", detail

        # Keep the original all-evidence rule for very flat rows.
        return orig_func(row)

    strict_low_vol_block._strict_low_vol_guard_v1 = True  # type: ignore[attr-defined]
    strict_low_vol_block._original = orig_func  # type: ignore[attr-defined]
    return strict_low_vol_block


def install() -> bool:
    global _INSTALLED, _ORIGINAL_LOW_VOL_BLOCK
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_LOW_VOLATILITY_GUARD_ENABLED", True):
        logger.warning("[LOW VOL STRICT GUARD] disabled by env")
        return False
    try:
        from . import low_volatility_entry_guard_patch as base

        try:
            base.install()
        except Exception:
            logger.debug("[LOW VOL STRICT GUARD] base install attempt failed", exc_info=True)

        cur = getattr(base, "_low_vol_block", None)
        if not callable(cur):
            logger.warning("[LOW VOL STRICT GUARD] base _low_vol_block unavailable")
            return False
        if getattr(cur, "_strict_low_vol_guard_v1", False):
            _INSTALLED = True
            return True

        _ORIGINAL_LOW_VOL_BLOCK = cur
        base._low_vol_block = _strict_low_vol_block_factory(base, cur)  # type: ignore[attr-defined]
        _INSTALLED = True
        logger.warning(
            "[LOW VOL STRICT GUARD] installed version=%s min_range=%.3f min_atr=%.3f min_change=%.3f",
            VERSION,
            _env_float("ENTRY_MIN_RANGE_PCT", 2.50),
            _env_float("ENTRY_MIN_ATR_PCT", 2.50),
            _env_float("ENTRY_MIN_ABS_CHANGE_PCT", 0.35),
        )
        return True
    except Exception:
        logger.exception("[LOW VOL STRICT GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[LOW VOL STRICT GUARD] auto install failed")


__all__ = ["VERSION", "install"]
