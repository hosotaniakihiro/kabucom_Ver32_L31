# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/ready_flags.py
# Version: PRODUCTION-STABLE-REV1.0-READY-FLAGS
# ------------------------------------------------------------
# 【概要】
#   未成熟indicatorの0表示抑制 / technical_ready / display_ready 付与
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .constants import MIN_TECH_READY_RSI, MIN_TECH_READY_MACD
from .dataframe_utils import ensure_df

logger = logging.getLogger(__name__)


def mask_unready_zero_indicators_to_nan(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    未成熟な indicator の 0 表示を避ける。
    """
    out = ensure_df(df)
    if out.empty:
        return out

    try:
        if "symbol_hist_len" not in out.columns:
            out["symbol_hist_len"] = out.groupby("symbol")["datetime"].transform("nunique").fillna(0).astype(int)

        hist = pd.to_numeric(out["symbol_hist_len"], errors="coerce").fillna(0)

        if int(interval) == 1:
            min_slope, min_rsi, min_macd, min_signal = 5, 14, 26, 34
        elif int(interval) == 3:
            min_slope, min_rsi, min_macd, min_signal = 3, 6, 10, 18
        elif int(interval) == 5:
            min_slope, min_rsi, min_macd, min_signal = 3, 5, 8, 12
        else:
            min_slope, min_rsi, min_macd, min_signal = 5, 14, 26, 34

        zero_cols_by_min = {
            "slope": min_slope,
            "slope_atr_scaled": min_slope,
            "score_slope": min_slope,
            "rsi": min_rsi,
            "macd": min_macd,
            "signal": min_signal,
            "hist": min_signal,
            "mtf": min_slope,
            "score_mtf": min_slope,
            "mtf_score": min_slope,
            "mtf_alignment": min_slope,
        }

        for col, min_hist in zero_cols_by_min.items():
            if col not in out.columns:
                continue

            s = pd.to_numeric(out[col], errors="coerce")
            mask = hist < min_hist
            out.loc[mask & s.fillna(0).eq(0), col] = np.nan

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] mask unready indicators failed interval=%s", interval, exc_info=True)

    return out


def attach_ready_flags(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_df(df)
    if out.empty:
        return out

    try:
        if "symbol_hist_len" not in out.columns:
            out["symbol_hist_len"] = out.groupby("symbol")["datetime"].transform("nunique").fillna(0).astype(int)

        hist_len = pd.to_numeric(out["symbol_hist_len"], errors="coerce").fillna(0)

        rsi_ok = (
            pd.to_numeric(out["rsi"], errors="coerce").notna()
            if "rsi" in out.columns
            else pd.Series(False, index=out.index)
        )
        macd_ok = (
            pd.to_numeric(out["macd"], errors="coerce").notna()
            if "macd" in out.columns
            else pd.Series(False, index=out.index)
        )
        signal_ok = (
            pd.to_numeric(out["signal"], errors="coerce").notna()
            if "signal" in out.columns
            else pd.Series(False, index=out.index)
        )

        slope_ok = pd.Series(False, index=out.index)
        for c in ("slope", "slope_atr_scaled", "score_slope"):
            if c in out.columns:
                slope_ok = slope_ok | pd.to_numeric(out[c], errors="coerce").notna()

        mtf_ok = pd.Series(False, index=out.index)
        for c in ("mtf", "score_mtf", "mtf_score", "mtf_alignment"):
            if c in out.columns:
                mtf_ok = mtf_ok | pd.to_numeric(out[c], errors="coerce").notna()

        out["technical_ready"] = (
            ((hist_len >= MIN_TECH_READY_MACD) & (macd_ok | signal_ok | slope_ok | mtf_ok))
            | ((hist_len >= MIN_TECH_READY_RSI) & (rsi_ok | slope_ok | mtf_ok))
        ).fillna(False).astype(int)

        score_ok = pd.Series(False, index=out.index)
        for c in (
            "score",
            "score_total",
            "display_score",
            "final_score",
            "score_buy",
            "score_sell",
            "buy_score",
            "sell_score",
        ):
            if c in out.columns:
                score_ok = score_ok | pd.to_numeric(out[c], errors="coerce").fillna(0).ne(0)

        close_ok = (
            pd.to_numeric(out["close"], errors="coerce").fillna(0).ne(0)
            if "close" in out.columns
            else pd.Series(False, index=out.index)
        )

        out["display_ready"] = (score_ok & close_ok).fillna(False).astype(int)

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] attach ready flags failed", exc_info=True)

    return out


__all__ = [
    "mask_unready_zero_indicators_to_nan",
    "attach_ready_flags",
]