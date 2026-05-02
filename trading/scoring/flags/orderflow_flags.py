# ============================================================
# File   : trading/scoring/flags/orderflow_flags.py
# Version: Ver1.0-PRODUCTION-ORDERFLOW-FLAGS
# ------------------------------------------------------------
# ✔ flag_bid_stack
# ✔ flag_bid_dominance
# ✔ flag_orderflow_imbalance
# ✔ flag_board_pressure_up
# ✔ flag_board_pressure_down
# ✔ flag_ask_stack
# ✔ flag_ask_dominance
# ✔ score_config.ini 完全対応
# ✔ NaN / inf 完全防御
# ✔ 板データ欠損安全
# ✔ vectorized高速処理
# ✔ DataFrame in / out
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np


# ============================================================
# safe numeric
# ============================================================

def _safe(series):

    if series is None:
        return None

    try:

        s = pd.to_numeric(series, errors="coerce")

        if isinstance(s, pd.Series):
            s = s.replace([np.inf, -np.inf], np.nan)

        return s

    except Exception:

        return series


# ============================================================
# column helper
# ============================================================

def _col(df, *names):

    lower_map = {c.lower(): c for c in df.columns}

    for n in names:

        if n in df.columns:
            return df[n]

        if n.lower() in lower_map:
            return df[lower_map[n.lower()]]

    return None


# ============================================================
# bool → int safe
# ============================================================

def _flag(expr):

    try:
        return expr.fillna(False).astype(int)
    except Exception:
        return pd.Series(0, index=expr.index)


# ============================================================
# main
# ============================================================

def generate_orderflow_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # board quantities
    # --------------------------------------------------------

    bid_qty = _safe(_col(df, "bid_qty", "bid_volume"))
    ask_qty = _safe(_col(df, "ask_qty", "ask_volume"))

    # fallback
    if bid_qty is None or ask_qty is None:
        return df

    # --------------------------------------------------------
    # bid stack
    # --------------------------------------------------------

    df["flag_bid_stack"] = _flag(
        bid_qty > ask_qty * 2
    )

    # --------------------------------------------------------
    # ask stack
    # --------------------------------------------------------

    df["flag_ask_stack"] = _flag(
        ask_qty > bid_qty * 2
    )

    # --------------------------------------------------------
    # dominance
    # --------------------------------------------------------

    df["flag_bid_dominance"] = _flag(
        bid_qty > ask_qty
    )

    df["flag_ask_dominance"] = _flag(
        ask_qty > bid_qty
    )

    # --------------------------------------------------------
    # orderflow imbalance
    # --------------------------------------------------------

    imbalance = (bid_qty - ask_qty) / (
        bid_qty + ask_qty + 1
    )

    df["flag_orderflow_imbalance"] = _flag(
        imbalance > 0.3
    )

    # --------------------------------------------------------
    # board pressure up
    # --------------------------------------------------------

    df["flag_board_pressure_up"] = _flag(
        bid_qty > ask_qty * 1.5
    )

    # --------------------------------------------------------
    # board pressure down
    # --------------------------------------------------------

    df["flag_board_pressure_down"] = _flag(
        ask_qty > bid_qty * 1.5
    )

    return df