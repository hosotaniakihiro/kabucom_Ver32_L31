# ============================================================
# File   : trading/entry/tonosama/ai_gate.py
# Version: Ver1.5-TONOSAMA-FALLBACK-NO-ZERO-5S-DIRECTION-GUARD
# ------------------------------------------------------------
# 目的:
#   AI未接続時の fallback が緩く、5秒変化0.000%や直近3m逆行でも
#   TONOSAMA ENTRY PENDING になる問題を防ぐ。
#
# Ver1.5:
#   - 5秒足が取れている場合、require_5s=Falseでも 5s=0.000% はNG。
#   - BUY fallback は 3m価格変化がマイナスならNG。
#   - SELL fallback は 3m価格変化がプラスならNG。
#   - BUY は 5mプラス + slopeプラスだけでは通さず、3mも最低0%以上を要求。
# ============================================================
from __future__ import annotations

import importlib
import logging
import os
import pandas as pd

from .utils import normalize_symbol, safe_float

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_float_floor(name: str, default: float, floor: float) -> float:
    try:
        return max(float(floor), _env_float(name, default))
    except Exception:
        return float(max(default, floor))


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _cfg(name: str, default):
    try:
        from . import config
        return getattr(config, name, default)
    except Exception:
        return default


def build_ai_features(row: pd.Series) -> dict:
    return {
        "symbol": normalize_symbol(row.get("symbol")),
        "symbolname": str(row.get("symbolname", "")),
        "price": safe_float(row.get("close"), 0.0),
        "raw_score": safe_float(row.get("_raw_score"), 0.0),
        "tonosama_score": safe_float(row.get("_tonosama_score"), 0.0),
        "volume_surge_ratio_3m": safe_float(row.get("volume_surge_ratio_3m"), 0.0),
        "volume_surge_ratio_5m": safe_float(row.get("volume_surge_ratio_5m"), 0.0),
        "price_change_pct_3m": safe_float(row.get("price_change_pct_3m"), 0.0),
        "price_change_pct_5m": safe_float(row.get("price_change_pct_5m"), 0.0),
        "max_volume_surge_ratio": safe_float(row.get("_max_volume_surge_ratio"), 0.0),
        "max_price_change_pct": safe_float(row.get("_max_price_change_pct"), 0.0),
        "has_5sec_bar": bool(row.get("has_5sec_bar", False)),
        "price_change_5s_pct": safe_float(row.get("price_change_5s_pct"), 0.0),
        "volume_surge_ratio_5s": safe_float(row.get("volume_surge_ratio_5s"), 0.0),
        "latest_5sec_close": safe_float(row.get("latest_5sec_close"), 0.0),
        "latest_5sec_volume": safe_float(row.get("latest_5sec_volume"), 0.0),
        "is_5sec_confirm_ok": bool(row.get("is_5sec_confirm_ok", False)),
        "slope": safe_float(row.get("_slope"), 0.0),
        "rsi": safe_float(row.get("rsi"), 0.0),
        "macd": safe_float(row.get("macd"), 0.0),
        "signal": safe_float(row.get("signal"), 0.0),
        "mtf": safe_float(row.get("mtf"), 0.0),
        "score_mtf": safe_float(row.get("score_mtf"), 0.0),
        "surge_tf": str(row.get("_surge_tf", "")),
    }


def _infer_side(max_chg: float, slope: float) -> str:
    if max_chg > 0 and slope > 0:
        return "BUY"
    if max_chg < 0 and slope < 0:
        return "SELL"
    if max_chg > 0 or slope > 0:
        return "BUY"
    if max_chg < 0 or slope < 0:
        return "SELL"
    return "UNKNOWN"


def _fallback_when_ai_disconnected(features: dict) -> tuple[bool, float, str]:
    max_surge = safe_float(features.get("max_volume_surge_ratio"), 0.0)
    max_chg = safe_float(features.get("max_price_change_pct"), 0.0)
    chg_3m = safe_float(features.get("price_change_pct_3m"), 0.0)
    chg_5m = safe_float(features.get("price_change_pct_5m"), 0.0)
    chg_5s = safe_float(features.get("price_change_5s_pct"), 0.0)
    has_5s = bool(features.get("has_5sec_bar", False))
    slope = safe_float(features.get("slope"), 0.0)

    side = _infer_side(max_chg, slope)
    abs_chg = abs(max_chg)
    abs_slope = abs(slope)

    min_surge = _env_float_floor("TONOSAMA_AI_FALLBACK_MIN_VOLUME_SURGE", float(_cfg("MIN_VOLUME_SURGE_RATIO", 3.0)), 3.0)
    min_chg = _env_float_floor("TONOSAMA_AI_FALLBACK_MIN_PRICE_CHANGE_PCT", float(_cfg("MIN_PRICE_CHANGE_PCT", 0.20)), 0.20)
    min_slope = _env_float_floor("TONOSAMA_AI_FALLBACK_MIN_SLOPE", float(_cfg("MIN_SLOPE", 0.0010)), 0.0010)
    min_5s = _env_float_floor("TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT", float(_cfg("MIN_5SEC_PRICE_CHANGE_PCT", 0.01)), 0.01)
    max_5s_drop = _env_float("TONOSAMA_AI_FALLBACK_MAX_5SEC_DROP_PCT", float(_cfg("MAX_5SEC_DROP_PCT", -0.20)))
    require_5s = _env_bool("TONOSAMA_AI_FALLBACK_REQUIRE_5SEC_BAR", bool(_cfg("REQUIRE_5SEC_BAR", False)))

    # Ver1.5: 5秒足があるのに0.000%なら、今は動いていないのでfallbackでは通さない。
    reject_zero_5s = _env_bool("TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC", True)
    min_buy_3m = _env_float("TONOSAMA_AI_FALLBACK_MIN_BUY_3M_CHANGE", 0.0)
    max_sell_3m = _env_float("TONOSAMA_AI_FALLBACK_MAX_SELL_3M_CHANGE", 0.0)

    if side == "UNKNOWN":
        return False, 0.0, f"AI fallback NG: unknown direction price_change={max_chg:.2f}% slope={slope:.4f}"
    if max_surge < min_surge:
        return False, 0.0, f"AI fallback NG: volume surge low side={side} max={max_surge:.2f}x < {min_surge:.2f}x"
    if abs_chg < min_chg:
        return False, 0.0, f"AI fallback NG: price change low side={side} abs={abs_chg:.2f}% raw={max_chg:.2f}% < {min_chg:.2f}%"
    if abs_slope < min_slope:
        return False, 0.0, f"AI fallback NG: slope low side={side} abs={abs_slope:.4f} raw={slope:.4f} < {min_slope:.4f}"

    if side == "BUY":
        if chg_3m < min_buy_3m:
            return False, 0.0, f"AI fallback NG: BUY 3m weak/reverse 3m={chg_3m:.2f}% < {min_buy_3m:.2f}% 5m={chg_5m:.2f}%"
        if max_chg <= 0 or slope <= 0:
            return False, 0.0, f"AI fallback NG: BUY direction mismatch max_chg={max_chg:.2f}% slope={slope:.4f}"
    elif side == "SELL":
        if chg_3m > max_sell_3m:
            return False, 0.0, f"AI fallback NG: SELL 3m weak/reverse 3m={chg_3m:.2f}% > {max_sell_3m:.2f}% 5m={chg_5m:.2f}%"
        if max_chg >= 0 or slope >= 0:
            return False, 0.0, f"AI fallback NG: SELL direction mismatch max_chg={max_chg:.2f}% slope={slope:.4f}"

    if has_5s:
        if reject_zero_5s and abs(chg_5s) < min_5s:
            return False, 0.0, f"AI fallback NG: 5s stopped side={side} abs5s={abs(chg_5s):.3f}% < {min_5s:.3f}%"
        if side == "BUY" and chg_5s <= max_5s_drop:
            return False, 0.0, f"AI fallback NG: BUY 5s reverse 5s={chg_5s:.3f}% <= {max_5s_drop:.3f}%"
        if side == "SELL" and chg_5s >= abs(max_5s_drop):
            return False, 0.0, f"AI fallback NG: SELL 5s reverse 5s={chg_5s:.3f}% >= {abs(max_5s_drop):.3f}%"
    elif require_5s:
        return False, 0.0, "AI fallback NG: missing required 5s bar"

    return True, 0.0, f"AI fallback pass side={side} surge={max_surge:.2f}x change={max_chg:.2f}% 3m={chg_3m:.2f}% 5m={chg_5m:.2f}% abs={abs_chg:.2f}% slope={slope:.4f} abs_slope={abs_slope:.4f} 5s={chg_5s:.3f}% require_5s={require_5s}"


def ai_check_tonosama_entry(row: pd.Series) -> tuple[bool, float, str]:
    features = build_ai_features(row)
    for module_name, func_name in [
        ("trading.entry.tonosama_ai", "judge_tonosama_entry"),
        ("trading.entry.tonosama_ai", "infer_tonosama_entry"),
        ("trading.entry.ignition.ai_boost", "judge_ai_boost_entry"),
    ]:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if not callable(fn):
                continue
            ret = fn(features)
            if isinstance(ret, tuple):
                ok = bool(ret[0])
                prob = safe_float(ret[1], 0.0) if len(ret) > 1 else (1.0 if ok else 0.0)
                reason = str(ret[2]) if len(ret) > 2 else func_name
                return ok, prob, reason
            if isinstance(ret, dict):
                ok = bool(ret.get("ok") or ret.get("entry") or ret.get("buy") or ret.get("decision"))
                prob = safe_float(ret.get("prob") or ret.get("confidence") or ret.get("ai_prob"), 1.0 if ok else 0.0)
                reason = str(ret.get("reason") or ret)
                return ok, prob, reason
            if isinstance(ret, bool):
                return ret, 1.0 if ret else 0.0, func_name
        except Exception:
            logger.warning("[TONOSAMA ENTRY AI] skipped module=%s func=%s", module_name, func_name, exc_info=True)

    return _fallback_when_ai_disconnected(features)
