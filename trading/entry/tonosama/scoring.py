# ============================================================
# File   : trading/entry/tonosama/scoring.py
# Version: Ver1.0-TONOSAMA-ENTRY-SCORING
# ============================================================
from __future__ import annotations
import pandas as pd
from trading.entry.final_entry_score import calc_final_entry_score
from .utils import safe_float

def prepare_entry_scores(x: pd.DataFrame) -> pd.DataFrame:
    if x is None or x.empty:
        return pd.DataFrame()
    out = x.copy()
    score_candidates = [c for c in ["final_score", "display_score", "disp_score", "score_total", "score_buy", "score", "ranking_score"] if c in out.columns]
    out["_raw_score"] = out[score_candidates].max(axis=1).fillna(0.0) if score_candidates else 0.0
    if "slope" in out.columns:
        out["_slope"] = pd.to_numeric(out["slope"], errors="coerce").fillna(0.0)
    elif "slope_atr_scaled" in out.columns:
        out["_slope"] = pd.to_numeric(out["slope_atr_scaled"], errors="coerce").fillna(0.0)
    else:
        out["_slope"] = 0.0
    out["_tonosama_score"] = out["_raw_score"].fillna(0.0) + out["_max_volume_surge_ratio"].fillna(0.0).clip(upper=6.0)*0.8 + out["_max_price_change_pct"].fillna(0.0).clip(lower=0.0, upper=5.0)*1.2 + out["_slope"].fillna(0.0).clip(lower=-0.02, upper=0.10)*5.0
    if "has_5sec_bar" in out.columns:
        out["_tonosama_score"] = out["_tonosama_score"] + out["price_change_5s_pct"].fillna(0.0).clip(lower=0.0, upper=1.0)*2.0
    return out

def calc_final_score_safe(row, *, raw_score: float, ai_prob: float) -> float:
    try:
        return safe_float(calc_final_entry_score(raw_score=raw_score, ma25_conf=row.get("ma25_conf") if "ma25_conf" in row.index else None, ma75_conf=row.get("ma75_conf") if "ma75_conf" in row.index else None, source="TONOSAMA", ai_prob=ai_prob if ai_prob > 0 else row.get("ai_prob", None)), 0.0)
    except Exception:
        return safe_float(raw_score, 0.0)
