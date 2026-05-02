# ============================================================
# trading/logger/institutional_logger.py
# Ver1.0-INSTITUTIONAL-MARKET-LOGGER
# ------------------------------------------------------------
# ✔ Smart Money Flow
# ✔ Liquidity Sweep
# ✔ Algo Spike
# ✔ Gamma Squeeze
# ✔ Trend Acceleration
# ✔ Market Regime
# ✔ symbol(symbolname)
# ✔ NaN / inf safe
# ✔ DataFrame safe
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .format_utils import (
    safe_copy,
    safe_symbolname,
    safe_numeric,
    fmt_float,
)

logger = logging.getLogger(__name__)


# ============================================================
# SMART MONEY FLOW
# ============================================================

def log_smart_money_flow(df, top_n=10):

    df = safe_copy(df)

    if df.empty:
        return

    if "orderflow_imbalance" not in df.columns:
        return

    df["flow"] = safe_numeric(df["orderflow_imbalance"])

    rank = df.sort_values("flow", ascending=False).head(top_n)

    if rank.empty:
        return

    logger.info("========== 🏦 SMART MONEY FLOW ==========")

    for i, r in enumerate(rank.itertuples(), 1):

        symbol = str(r.symbol)
        name = safe_symbolname(r)

        logger.info(
            "%2d. %s(%s) flow=%.2f",
            i,
            symbol,
            name,
            float(r.flow),
        )


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def log_liquidity_sweep(df):

    df = safe_copy(df)

    if df.empty:
        return

    if "liquidity_grab" not in df.columns:
        return

    sweep = df[df["liquidity_grab"] == 1]

    if sweep.empty:
        return

    logger.info("========== 🧹 LIQUIDITY SWEEP ==========")

    for r in sweep.itertuples():

        symbol = str(r.symbol)
        name = safe_symbolname(r)

        logger.info(
            "%s(%s) liquidity sweep detected",
            symbol,
            name,
        )


# ============================================================
# ALGO SPIKE
# ============================================================

def log_algo_spike(df):

    df = safe_copy(df)

    if df.empty:
        return

    if "algo_spike" not in df.columns:
        return

    spikes = df[df["algo_spike"] == 1]

    if spikes.empty:
        return

    logger.info("========== ⚡ ALGO SPIKE ==========")

    for r in spikes.itertuples():

        symbol = str(r.symbol)
        name = safe_symbolname(r)

        logger.info(
            "%s(%s) algorithmic spike detected",
            symbol,
            name,
        )


# ============================================================
# GAMMA SQUEEZE
# ============================================================

def log_gamma_squeeze(df, threshold=3.0):

    df = safe_copy(df)

    if df.empty:
        return

    if "gamma_pressure" not in df.columns:
        return

    df["gamma"] = safe_numeric(df["gamma_pressure"])

    squeeze = df[df["gamma"] >= threshold]

    if squeeze.empty:
        return

    logger.info("========== 💥 GAMMA SQUEEZE ==========")

    for r in squeeze.itertuples():

        symbol = str(r.symbol)
        name = safe_symbolname(r)

        logger.info(
            "%s(%s) gamma pressure=%.2f",
            symbol,
            name,
            float(r.gamma),
        )


# ============================================================
# TREND ACCELERATION
# ============================================================

def log_trend_acceleration(df, top_n=10):

    df = safe_copy(df)

    if df.empty:
        return

    if "ma75_slope" not in df.columns:
        return

    df["slope"] = safe_numeric(df["ma75_slope"])

    rank = df.sort_values("slope", ascending=False).head(top_n)

    if rank.empty:
        return

    logger.info("========== 🚀 TREND ACCELERATION ==========")

    for i, r in enumerate(rank.itertuples(), 1):

        symbol = str(r.symbol)
        name = safe_symbolname(r)

        logger.info(
            "%2d. %s(%s) slope=%.4f",
            i,
            symbol,
            name,
            float(r.slope),
        )


# ============================================================
# MARKET REGIME
# ============================================================

def log_market_regime(regime):

    try:

        if regime is None:
            return

        logger.info("========== 🌍 MARKET REGIME ==========")

        logger.info("Current regime: %s", regime)

    except Exception:

        logger.exception("[MARKET REGIME LOGGER ERROR]")