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


def _rescue(row, reason):
    if not _on("TONOSAMA_FAILOPEN_DIRECTION_RESCUE", True):
        return False, "disabled"
    r = str(reason or "")
    if "price change low" not in r or "range_rescue_direction_ng" not in r:
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
    if surge < min_surge or vol < min_vol or rng < min_range or abs(slope) < min_slope:
        return False, f"weak surge={surge:.2f} vol={vol:.0f} range={rng:.3f} slope={slope:.6f}"
    if not failopen and _on("TONOSAMA_FAILOPEN_DIRECTION_RESCUE_REQUIRE_FAILOPEN", True):
        return False, "not_failopen"
    if slope > 0:
        side = "BUY"
        # 買いは上ヒゲだらけなら買いクライマックスとして救済しない
        if close_pos < 35.0 or upper > 85.0:
            return False, f"buy_position_ng close_pos={close_pos:.1f} upper={upper:.1f}"
    else:
        side = "SELL"
        # 売りは下ヒゲだらけなら売りクライマックスとして救済しない
        if close_pos > 65.0 or lower > 85.0:
            return False, f"sell_position_ng close_pos={close_pos:.1f} lower={lower:.1f}"
    return True, f"failopen_direction_rescue side={side} surge={surge:.2f} vol={vol:.0f} range={rng:.3f} slope={slope:.6f} close_pos={close_pos:.1f}"


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
        if getattr(cur, "_tonosama_failopen_direction_rescue_v1", False):
            _INSTALLED = True
            return True
        _ORIG = cur
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
        patched._tonosama_failopen_direction_rescue_v1 = True
        patched._original = cur
        runner.ai_check_tonosama_entry = patched
        ai_gate.ai_check_tonosama_entry = patched
        _INSTALLED = True
        logger.warning("[TONOSAMA FAILOPEN DIRECTION RESCUE] installed V1")
        return True
    except Exception:
        logger.exception("[TONOSAMA FAILOPEN DIRECTION RESCUE] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[TONOSAMA FAILOPEN DIRECTION RESCUE] auto install failed")

__all__ = ["install"]
