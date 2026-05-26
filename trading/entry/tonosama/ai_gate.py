# ============================================================
# File   : trading/entry/tonosama/ai_gate.py
# Version: Ver1.2-TONOSAMA-FALLBACK-CONFIG-ALIGNED
# ------------------------------------------------------------
# AI判定モジュールが利用できない場合の代替判定。
# runner/config.py 側の殿様条件と同じ閾値を使う。
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


def _cfg(name: str, default: float) -> float:
    try:
        from . import config
        return float(getattr(config, name, default))
    except Exception:
        return float(default)


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


def _fallback_when_ai_disconnected(features: dict) -> tuple[bool, float, str]:
    max_surge = safe_float(features.get("max_volume_surge_ratio"), 0.0)
    max_chg = safe_float(features.get("max_price_change_pct"), 0.0)
    chg_5s = safe_float(features.get("price_change_5s_pct"), 0.0)
    has_5s = bool(features.get("has_5sec_bar", False))
    slope = safe_float(features.get("slope"), 0.0)

    min_surge = _env_float("TONOSAMA_AI_FALLBACK_MIN_VOLUME_SURGE", _cfg("MIN_VOLUME_SURGE_RATIO", 3.0))
    min_chg = _env_float("TONOSAMA_AI_FALLBACK_MIN_PRICE_CHANGE_PCT", _cfg("MIN_PRICE_CHANGE_PCT", 0.05))
    min_slope = _env_float("TONOSAMA_AI_FALLBACK_MIN_SLOPE", _cfg("MIN_SLOPE", 0.0003))
    min_5s = _env_float("TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT", _cfg("MIN_5SEC_PRICE_CHANGE_PCT", 0.01))

    if max_surge < min_surge:
        return False, 0.0, f"fallback volume surge low max={max_surge:.2f}x < {min_surge:.2f}x"
    if max_chg < min_chg:
        return False, 0.0, f"fallback price change low max={max_chg:.2f}% < {min_chg:.2f}%"
    if slope < min_slope:
        return False, 0.0, f"fallback slope low slope={slope:.4f} < {min_slope:.4f}"
    if has_5s and chg_5s < min_5s:
        return False, 0.0, f"fallback 5sec change low 5s={chg_5s:.3f}% < {min_5s:.3f}%"

    return True, 0.0, f"fallback rule pass surge={max_surge:.2f}x chg={max_chg:.2f}% slope={slope:.4f}"


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
