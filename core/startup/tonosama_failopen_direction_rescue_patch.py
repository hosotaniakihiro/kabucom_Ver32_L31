from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG = None


def _on(name, default=True):
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _f(v, d=0.0):
    try:
        if v is None or str(v).strip() == "":
            return float(d)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(d)


def _infer_side_from_shape(close_pos: float, upper: float, lower: float) -> tuple[str | None, str]:
    """Infer direction when price_change/slope are zero.

    SELL shape: close near low + large upper wick.
    BUY shape : close near high + large lower wick.
    """
    sell_close_max = _f(os.getenv("TONOSAMA_SHAPE_DIRECTION_SELL_CLOSE_POS_MAX"), 35.0)
    sell_upper_min = _f(os.getenv("TONOSAMA_SHAPE_DIRECTION_SELL_UPPER_MIN"), 60.0)
    buy_close_min = _f(os.getenv("TONOSAMA_SHAPE_DIRECTION_BUY_CLOSE_POS_MIN"), 65.0)
    buy_lower_min = _f(os.getenv("TONOSAMA_SHAPE_DIRECTION_BUY_LOWER_MIN"), 60.0)
    if close_pos <= sell_close_max and upper >= sell_upper_min:
        return "SELL", f"shape_sell close_pos={close_pos:.1f} upper={upper:.1f}"
    if close_pos >= buy_close_min and lower >= buy_lower_min:
        return "BUY", f"shape_buy close_pos={close_pos:.1f} lower={lower:.1f}"
    return None, f"shape_unknown close_pos={close_pos:.1f} upper={upper:.1f} lower={lower:.1f}"


def _rescue(row, reason):
    if not _on("TONOSAMA_FAILOPEN_DIRECTION_RESCUE", True):
        return False, "disabled"
    r = str(reason or "")
    target = (
        ("price change low" in r and "range_rescue_direction_ng" in r)
        or ("unknown direction" in r and "price_change=0.00" in r)
    )
    if not target:
        return False, "not_target"

    surge = _f(row.get("_max_volume_surge_ratio"), 0.0)
    vol = max(_f(row.get("_latest_volume"), 0.0), _f(row.get("volume"), 0.0))
    rng = _f(row.get("_intrabar_range_pct"), 0.0)
    slope = _f(row.get("_slope"), 0.0)
    close_pos = _f(row.get("_close_position_pct"), 50.0)
    upper = _f(row.get("_upper_wick_pct"), 0.0)
    lower = _f(row.get("_lower_wick_pct"), 0.0)
    failopen = bool(row.get("_volume_surge_failopen", False) or row.get("_volume_surge_history_missing", False))

    min_surge = _f(os.getenv("TONOSAMA_FAILOPEN_DIRECTION_RESCUE_MIN_SURGE"), 3.0)
    min_vol = _f(os.getenv("TONOSAMA_FAILOPEN_DIRECTION_RESCUE_MIN_VOLUME"), 500000.0)
    min_range = _f(os.getenv("TONOSAMA_FAILOPEN_DIRECTION_RESCUE_MIN_RANGE_PCT"), 5.0)
    min_slope = _f(os.getenv("TONOSAMA_FAILOPEN_DIRECTION_RESCUE_MIN_SLOPE_ABS"), 0.0005)
    allow_shape = _on("TONOSAMA_FAILOPEN_DIRECTION_RESCUE_ALLOW_SHAPE", True)

    if surge < min_surge or vol < min_vol or rng < min_range:
        return False, f"weak surge={surge:.2f} vol={vol:.0f} range={rng:.3f}"
    if not failopen and _on("TONOSAMA_FAILOPEN_DIRECTION_RESCUE_REQUIRE_FAILOPEN", True):
        return False, "not_failopen"

    side = None
    detail = ""
    if abs(slope) >= min_slope:
        if slope > 0:
            side = "BUY"
            if close_pos < 35.0 or upper > 85.0:
                return False, f"buy_position_ng close_pos={close_pos:.1f} upper={upper:.1f} slope={slope:.6f}"
        else:
            side = "SELL"
            if close_pos > 65.0 or lower > 85.0:
                return False, f"sell_position_ng close_pos={close_pos:.1f} lower={lower:.1f} slope={slope:.6f}"
        detail = f"slope_direction slope={slope:.6f}"
    elif allow_shape:
        side, detail = _infer_side_from_shape(close_pos, upper, lower)
        if not side:
            return False, detail + f" slope={slope:.6f}"
    else:
        return False, f"slope_weak slope={slope:.6f} min={min_slope:.6f}"

    return True, f"failopen_direction_rescue side={side} {detail} surge={surge:.2f} vol={vol:.0f} range={rng:.3f} slope={slope:.6f} close_pos={close_pos:.1f} upper={upper:.1f} lower={lower:.1f}"


def install():
    global _INSTALLED, _ORIG
    if _INSTALLED:
        return True
    try:
        import trading.entry.tonosama.runner as runner
        import trading.entry.tonosama.ai_gate as ai_gate
        cur = getattr(runner, "ai_check_tonosama_entry", None)
        if not callable(cur):
            return False
        if getattr(cur, "_tonosama_failopen_direction_rescue_v2", False):
            _INSTALLED = True
            return True
        _ORIG = getattr(cur, "_original", cur)

        def patched(row):
            ok, prob, reason = _ORIG(row)
            if ok:
                return ok, prob, reason
            try:
                rescue, detail = _rescue(row, reason)
                symbol = row.get("symbol", "") if hasattr(row, "get") else ""
                if rescue:
                    logger.warning("[TONOSAMA FAILOPEN DIRECTION RESCUE] OK symbol=%s original=%s detail=%s", symbol, reason, detail)
                    return True, max(_f(prob, 0.0), 0.0), "AI failopen direction rescue: " + detail
                logger.info("[TONOSAMA FAILOPEN DIRECTION RESCUE] keep NG symbol=%s original=%s detail=%s", symbol, reason, detail)
            except Exception:
                logger.debug("[TONOSAMA FAILOPEN DIRECTION RESCUE] failed", exc_info=True)
            return ok, prob, reason

        patched._tonosama_failopen_direction_rescue_v2 = True
        patched._tonosama_failopen_direction_rescue_v1 = True
        patched._original = _ORIG
        runner.ai_check_tonosama_entry = patched
        ai_gate.ai_check_tonosama_entry = patched
        _INSTALLED = True
        logger.warning("[TONOSAMA FAILOPEN DIRECTION RESCUE] installed V2 shape_direction=%s", _on("TONOSAMA_FAILOPEN_DIRECTION_RESCUE_ALLOW_SHAPE", True))
        return True
    except Exception:
        logger.exception("[TONOSAMA FAILOPEN DIRECTION RESCUE] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[TONOSAMA FAILOPEN DIRECTION RESCUE] auto install failed")

__all__ = ["install"]
