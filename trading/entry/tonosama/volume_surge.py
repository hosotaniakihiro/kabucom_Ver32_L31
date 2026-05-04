# ============================================================
# File   : trading/entry/tonosama/volume_surge.py
# Version: Ver1.0-TONOSAMA-ENTRY-VOLUME-SURGE
# ============================================================
from __future__ import annotations
import pandas as pd
from .config import VOLUME_AVG_LOOKBACK_BARS
from .summary_loader import load_merged_summary, normalize_summary_base
from .utils import safe_float

def add_volume_surge_features(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    x = normalize_summary_base(df, interval=interval)
    if x.empty:
        return pd.DataFrame()
    x = x.sort_values(["symbol", "datetime"])
    g = x.groupby("symbol", group_keys=False)
    avg_col = f"prev{VOLUME_AVG_LOOKBACK_BARS}_volume_avg_{interval}m"
    ratio_col = f"volume_surge_ratio_{interval}m"
    x[avg_col] = g["volume"].transform(lambda s: s.shift(1).rolling(VOLUME_AVG_LOOKBACK_BARS, min_periods=2).mean())
    x[ratio_col] = x["volume"] / x[avg_col].replace(0, pd.NA)
    x[ratio_col] = pd.to_numeric(x[ratio_col], errors="coerce").replace([float("inf"), -float("inf")], pd.NA)
    prev_close_col = f"prev_close_{interval}m"
    price_chg_col = f"price_change_pct_{interval}m"
    x[prev_close_col] = g["close"].shift(1)
    x[price_chg_col] = ((x["close"] - x[prev_close_col]) / x[prev_close_col].replace(0, pd.NA) * 100.0)
    latest = x.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"]).groupby("symbol", group_keys=False).tail(1)
    keep_cols = ["symbol", "datetime", "close", "volume", avg_col, ratio_col, prev_close_col, price_chg_col]
    for c in keep_cols:
        if c not in latest.columns:
            latest[c] = pd.NA
    latest = latest[keep_cols].copy().rename(columns={"datetime": f"datetime_{interval}m", "close": f"close_{interval}m", "volume": f"volume_{interval}m"})
    return latest.reset_index(drop=True)

def build_scalping_feature_df() -> pd.DataFrame:
    df1 = normalize_summary_base(load_merged_summary(1), interval=1)
    df3 = add_volume_surge_features(load_merged_summary(3), interval=3)
    df5 = add_volume_surge_features(load_merged_summary(5), interval=5)
    if df1.empty:
        return pd.DataFrame()
    out = df1.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"]).groupby("symbol", group_keys=False).tail(1).copy()
    if out.empty:
        return pd.DataFrame()
    if not df3.empty:
        out = out.merge(df3, on="symbol", how="left")
    if not df5.empty:
        out = out.merge(df5, on="symbol", how="left")
    for c in ["volume_surge_ratio_3m", "volume_surge_ratio_5m", "price_change_pct_3m", "price_change_pct_5m", "prev5_volume_avg_3m", "prev5_volume_avg_5m", "volume_3m", "volume_5m"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    vol_cols = [c for c in ["volume_surge_ratio_3m", "volume_surge_ratio_5m"] if c in out.columns]
    price_cols = [c for c in ["price_change_pct_3m", "price_change_pct_5m"] if c in out.columns]
    out["_max_volume_surge_ratio"] = out[vol_cols].max(axis=1) if vol_cols else 0.0
    out["_max_price_change_pct"] = out[price_cols].max(axis=1) if price_cols else 0.0
    out["_surge_tf"] = ""
    if "volume_surge_ratio_3m" in out.columns and "volume_surge_ratio_5m" in out.columns:
        out["_surge_tf"] = out.apply(lambda r: "3m" if safe_float(r.get("volume_surge_ratio_3m"), 0) >= safe_float(r.get("volume_surge_ratio_5m"), 0) else "5m", axis=1)
    elif "volume_surge_ratio_3m" in out.columns:
        out["_surge_tf"] = "3m"
    elif "volume_surge_ratio_5m" in out.columns:
        out["_surge_tf"] = "5m"
    return out.reset_index(drop=True)
