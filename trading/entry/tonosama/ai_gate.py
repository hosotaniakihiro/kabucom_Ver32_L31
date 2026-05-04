# ============================================================
# File   : trading/entry/tonosama/ai_gate.py
# Version: Ver1.0-TONOSAMA-ENTRY-AI-GATE
# ============================================================
from __future__ import annotations
import importlib, logging
import pandas as pd
from .utils import normalize_symbol, safe_float
logger = logging.getLogger(__name__)

def build_ai_features(row: pd.Series) -> dict:
    return {"symbol": normalize_symbol(row.get("symbol")), "symbolname": str(row.get("symbolname", "")), "price": safe_float(row.get("close"), 0.0), "raw_score": safe_float(row.get("_raw_score"), 0.0), "tonosama_score": safe_float(row.get("_tonosama_score"), 0.0), "volume_surge_ratio_3m": safe_float(row.get("volume_surge_ratio_3m"), 0.0), "volume_surge_ratio_5m": safe_float(row.get("volume_surge_ratio_5m"), 0.0), "price_change_pct_3m": safe_float(row.get("price_change_pct_3m"), 0.0), "price_change_pct_5m": safe_float(row.get("price_change_pct_5m"), 0.0), "max_volume_surge_ratio": safe_float(row.get("_max_volume_surge_ratio"), 0.0), "max_price_change_pct": safe_float(row.get("_max_price_change_pct"), 0.0), "has_5sec_bar": bool(row.get("has_5sec_bar", False)), "price_change_5s_pct": safe_float(row.get("price_change_5s_pct"), 0.0), "volume_surge_ratio_5s": safe_float(row.get("volume_surge_ratio_5s"), 0.0), "latest_5sec_close": safe_float(row.get("latest_5sec_close"), 0.0), "latest_5sec_volume": safe_float(row.get("latest_5sec_volume"), 0.0), "is_5sec_confirm_ok": bool(row.get("is_5sec_confirm_ok", False)), "slope": safe_float(row.get("_slope"), 0.0), "rsi": safe_float(row.get("rsi"), 0.0), "macd": safe_float(row.get("macd"), 0.0), "signal": safe_float(row.get("signal"), 0.0), "mtf": safe_float(row.get("mtf"), 0.0), "score_mtf": safe_float(row.get("score_mtf"), 0.0), "surge_tf": str(row.get("_surge_tf", ""))}

def ai_check_tonosama_entry(row: pd.Series) -> tuple[bool, float, str]:
    features = build_ai_features(row)
    for module_name, func_name in [("trading.entry.tonosama_ai", "judge_tonosama_entry"), ("trading.entry.tonosama_ai", "infer_tonosama_entry"), ("trading.entry.ignition.ai_boost", "judge_ai_boost_entry")]:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if not callable(fn):
                continue
            ret = fn(features)
            if isinstance(ret, tuple):
                ok = bool(ret[0]); prob = safe_float(ret[1], 0.0) if len(ret) > 1 else (1.0 if ok else 0.0); reason = str(ret[2]) if len(ret) > 2 else func_name
                return ok, prob, reason
            if isinstance(ret, dict):
                ok = bool(ret.get("ok") or ret.get("entry") or ret.get("buy") or ret.get("decision")); prob = safe_float(ret.get("prob") or ret.get("confidence") or ret.get("ai_prob"), 1.0 if ok else 0.0); reason = str(ret.get("reason") or ret)
                return ok, prob, reason
            if isinstance(ret, bool):
                return ret, 1.0 if ret else 0.0, func_name
        except Exception:
            logger.warning("[TONOSAMA ENTRY AI] skipped module=%s func=%s", module_name, func_name, exc_info=True)
    return True, 0.0, "ai_not_connected_rule_pass"
