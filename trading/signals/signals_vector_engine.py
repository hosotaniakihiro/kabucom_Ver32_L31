# ============================================================
# trading/signals/signals_vector_engine.py
# Ver1.0-VECTOR-SIGNALS-ENGINE
# ------------------------------------------------------------
# ✔ vectorized signal evaluation
# ✔ BUY / SHORT simultaneous evaluation
# ✔ pandas高速処理
# ✔ KeyError / NaN guard
# ✔ summary_engine compatible
# ✔ large universe ready (500+ symbols)
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.signals.price_normalizer import normalize_dataframe

logger = logging.getLogger(__name__)


# ============================================================
# SAFE COLUMN
# ============================================================

def _col(df, name, default=0):

    if name not in df.columns:

        df[name] = default

    return df[name]


# ============================================================
# BASIC VECTOR SIGNALS
# ============================================================

def _vector_signals(df):

    df = df.copy()

    close = _col(df, "close")
    ma5 = _col(df, "ma5")
    ma25 = _col(df, "ma25")
    ma75 = _col(df, "ma75")
    macd = _col(df, "macd")
    signal = _col(df, "signal")
    rsi = _col(df, "rsi")
    vwap = _col(df, "vwap")

    # --------------------------------------------------------
    # BUY conditions
    # --------------------------------------------------------

    df["sig_ma_uptrend"] = (ma5 > ma25) & (ma25 > ma75)

    df["sig_ma_cross"] = (ma5 > ma25) & (ma5.shift(1) <= ma25.shift(1))

    df["sig_macd_cross"] = (macd > signal) & (macd.shift(1) <= signal.shift(1))

    df["sig_rsi_rebound"] = (rsi > rsi.shift(1)) & (rsi.shift(1) < 30)

    df["sig_vwap_break"] = close > vwap

    # --------------------------------------------------------
    # SHORT conditions
    # --------------------------------------------------------

    df["sig_ma_downtrend"] = (ma5 < ma25) & (ma25 < ma75)

    df["sig_macd_dc"] = (macd < signal) & (macd.shift(1) >= signal.shift(1))

    df["sig_rsi_drop"] = rsi < rsi.shift(1)

    df["sig_vwap_fail"] = close < vwap

    return df


# ============================================================
# BUILD SIGNAL LIST
# ============================================================

def _build_signal_lists(df):

    buy_cols = [
        "sig_ma_uptrend",
        "sig_ma_cross",
        "sig_macd_cross",
        "sig_rsi_rebound",
        "sig_vwap_break",
    ]

    short_cols = [
        "sig_ma_downtrend",
        "sig_macd_dc",
        "sig_rsi_drop",
        "sig_vwap_fail",
    ]

    buy_lists = []
    short_lists = []

    for i, row in df.iterrows():

        buy = [c.replace("sig_", "") for c in buy_cols if row.get(c)]

        short = [c.replace("sig_", "") for c in short_cols if row.get(c)]

        buy_lists.append(buy)
        short_lists.append(short)

    df["buy_signals"] = buy_lists
    df["short_signals"] = short_lists

    return df


# ============================================================
# DECISION
# ============================================================

def _resolve_decision(df):

    buy_strength = df["buy_signals"].apply(len)
    short_strength = df["short_signals"].apply(len)

    decision = []

    for b, s in zip(buy_strength, short_strength):

        if b > s:
            decision.append("BUY")

        elif s > b:
            decision.append("SHORT")

        else:
            decision.append(None)

    df["signal_decision"] = decision

    return df


# ============================================================
# SYMBOL PROCESSOR
# ============================================================

def _process_symbol(symbol_df):

    try:

        symbol_df = normalize_dataframe(symbol_df)

        symbol_df = _vector_signals(symbol_df)

        symbol_df = _build_signal_lists(symbol_df)

        symbol_df = _resolve_decision(symbol_df)

    except Exception:

        logger.exception("[VECTOR SIGNAL] symbol processing failed")

    return symbol_df


# ============================================================
# MAIN VECTOR ENGINE
# ============================================================

def run_vector_signals(df):

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:

        logger.warning("[VECTOR SIGNAL] symbol column missing")

        return df

    outputs = []

    grouped = df.groupby("symbol")

    for symbol, g in grouped:

        g = g.sort_values("datetime")

        processed = _process_symbol(g)

        outputs.append(processed)

    try:

        df_out = pd.concat(outputs)

    except Exception:

        logger.exception("[VECTOR SIGNAL] concat failed")

        return df

    return df_out


# ============================================================
# FAST LATEST SIGNAL
# ============================================================

def latest_signal(df):

    if df is None or len(df) == 0:
        return None

    df = run_vector_signals(df)

    row = df.iloc[-1]

    return {
        "buy_signals": row.get("buy_signals", []),
        "short_signals": row.get("short_signals", []),
        "decision": row.get("signal_decision"),
    }