# ============================================================
# File   : trading/entry/tonosama/summary_loader.py
# Version: Ver1.0-TONOSAMA-ENTRY-SUMMARY-LOADER
# ============================================================
from __future__ import annotations
import logging
import pandas as pd
from global_state import global_data
from .utils import normalize_symbol, first_existing_col
logger = logging.getLogger(__name__)

def load_merged_summary(interval: int) -> pd.DataFrame:
    try:
        df = global_data.get_merged_summary(int(interval)) if hasattr(global_data, "get_merged_summary") else None
        if df is None or getattr(df, "empty", True):
            logger.info("[TONOSAMA ENTRY] merged summary empty interval=%s", interval)
            return pd.DataFrame()
        if not isinstance(df, pd.DataFrame):
            logger.warning("[TONOSAMA ENTRY] merged summary is not DataFrame interval=%s type=%s", interval, type(df).__name__)
            return pd.DataFrame()
        out = df.copy()
        out["_interval"] = int(interval)
        return out
    except Exception:
        logger.exception("[TONOSAMA ENTRY] load merged summary failed interval=%s", interval)
        return pd.DataFrame()

def normalize_summary_base(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x["_interval"] = int(interval)
    if "symbol" not in x.columns:
        return pd.DataFrame()
    x["symbol"] = x["symbol"].map(normalize_symbol)
    x = x[x["symbol"] != ""]
    if x.empty:
        return pd.DataFrame()
    if "symbolname" not in x.columns:
        name_col = first_existing_col(x, ["name", "symbol_name", "SymbolName"])
        x["symbolname"] = x[name_col].astype(str) if name_col else ""
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce") if "datetime" in x.columns else pd.NaT
    close_col = first_existing_col(x, ["close", "close_price", "current_price", "price", "last_price"])
    if close_col is None:
        return pd.DataFrame()
    volume_col = first_existing_col(x, ["volume", "trading_volume", "Volume"])
    x["volume"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0
    x["close"] = pd.to_numeric(x[close_col], errors="coerce")
    x = x.dropna(subset=["close"])
    x = x[x["close"] > 0]
    numeric_cols = ["score", "score_buy", "score_total", "final_score", "display_score", "disp_score", "ranking_score", "rsi", "macd", "signal", "slope", "slope_atr_scaled", "mtf", "score_mtf", "mtf_score", "ma5", "ma25", "ma75", "ma25_conf", "ma75_conf", "ai_prob", "ranking_momentum", "rank_improve", "volume_delta", "change_percentage"]
    for c in numeric_cols:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x.sort_values(["symbol", "datetime"])
