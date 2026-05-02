# ============================================================
# trading/summary/institutional_logger.py
# Ver1.1-INSTITUTIONAL-MARKET-LOGGER-PRODUCTION-STABLE
# ------------------------------------------------------------
# ✔ Momentum ranking
# ✔ Liquidity ranking
# ✔ Tosama Inago signals
# ✔ OrderFlow imbalance
# ✔ VWAP deviation
# ✔ AI meta score
# ✔ NaN / inf safe
# ✔ DataFrame / list safe
# ✔ symbolname auto resolve
# ✔ production safe
# ✔ column compatibility absorb
# ✔ logging safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# SAFE UTIL
# ============================================================

def _safe_df(df):

    if df is None:
        return pd.DataFrame()

    if isinstance(df, list):

        if not df:
            return pd.DataFrame()

        return pd.DataFrame(df)

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    return df.copy()


def _safe_series(df, col):

    if col not in df.columns:
        return pd.Series(0.0, index=df.index)

    try:

        s = pd.to_numeric(df[col], errors="coerce")

        s = s.replace([float("inf"), float("-inf")], 0)

        return s.fillna(0)

    except Exception:

        return pd.Series(0.0, index=df.index)


def _safe_symbol(df):

    if "symbol" not in df.columns:
        return False

    df["symbol"] = df["symbol"].astype(str)

    return True


def _symbol_name(symbol):

    try:

        name = global_data.symbol_name_map.get(symbol)

        if name:
            return name

        return symbol

    except Exception:

        return symbol


def _safe_close(df):

    for col in ("close", "close_price", "c"):
        if col in df.columns:
            return _safe_series(df, col)

    return pd.Series(0.0, index=df.index)


def _safe_volume(df):

    for col in ("volume", "vol"):
        if col in df.columns:
            return _safe_series(df, col)

    return pd.Series(0.0, index=df.index)


def _safe_vwap(df):

    if "vwap" in df.columns:
        return _safe_series(df, "vwap")

    return pd.Series(0.0, index=df.index)


def _safe_ai_score(df):

    if "ai_score" in df.columns:
        return _safe_series(df, "ai_score")

    if "ai_meta_score" in df.columns:
        return _safe_series(df, "ai_meta_score")

    return pd.Series(0.0, index=df.index)


# ============================================================
# MOMENTUM
# ============================================================

def log_momentum_leaders(df, top_n=10):

    df = _safe_df(df)

    if df.empty:
        return

    if not _safe_symbol(df):
        return

    if "ma75_slope" not in df.columns:
        return

    df["momentum"] = _safe_series(df, "ma75_slope")

    rank = (
        df.sort_values("momentum", ascending=False)
        .drop_duplicates("symbol")
        .head(top_n)
    )

    if rank.empty:
        return

    logger.info("========== 📊 Momentum Leaders ==========")

    for i, r in enumerate(rank.itertuples(), 1):

        symbol = str(r.symbol)
        name = _symbol_name(symbol)

        logger.info(
            "%2d. %s(%s) slope=%.4f",
            i,
            symbol,
            name,
            float(r.momentum),
        )


# ============================================================
# LIQUIDITY
# ============================================================

def log_liquidity_leaders(df, top_n=10):

    df = _safe_df(df)

    if df.empty:
        return

    if not _safe_symbol(df):
        return

    df["volume_safe"] = _safe_volume(df)

    rank = (
        df.sort_values("volume_safe", ascending=False)
        .drop_duplicates("symbol")
        .head(top_n)
    )

    if rank.empty:
        return

    logger.info("========== 📊 Liquidity Leaders ==========")

    for i, r in enumerate(rank.itertuples(), 1):

        symbol = str(r.symbol)
        name = _symbol_name(symbol)

        logger.info(
            "%2d. %s(%s) volume=%s",
            i,
            symbol,
            name,
            f"{int(r.volume_safe):,}",
        )


# ============================================================
# VWAP DEVIATION
# ============================================================

def log_vwap_deviation(df, top_n=10):

    df = _safe_df(df)

    if df.empty:
        return

    if not _safe_symbol(df):
        return

    close = _safe_close(df)
    vwap = _safe_vwap(df)

    if vwap.sum() == 0:
        return

    df["vwap_dev"] = (close - vwap) / vwap

    rank = (
        df.sort_values("vwap_dev", ascending=False)
        .drop_duplicates("symbol")
        .head(top_n)
    )

    if rank.empty:
        return

    logger.info("========== 📊 VWAP Deviation ==========")

    for i, r in enumerate(rank.itertuples(), 1):

        symbol = str(r.symbol)
        name = _symbol_name(symbol)

        logger.info(
            "%2d. %s(%s) dev=%.3f%%",
            i,
            symbol,
            name,
            float(r.vwap_dev * 100),
        )


# ============================================================
# ORDERFLOW IMBALANCE
# ============================================================

def log_orderflow_imbalance(df, top_n=10):

    df = _safe_df(df)

    if df.empty:
        return

    if not _safe_symbol(df):
        return

    if "orderflow_imbalance" not in df.columns:
        return

    df["imbalance_safe"] = _safe_series(df, "orderflow_imbalance")

    rank = (
        df.sort_values("imbalance_safe", ascending=False)
        .drop_duplicates("symbol")
        .head(top_n)
    )

    if rank.empty:
        return

    logger.info("========== 📊 OrderFlow Imbalance ==========")

    for i, r in enumerate(rank.itertuples(), 1):

        symbol = str(r.symbol)
        name = _symbol_name(symbol)

        logger.info(
            "%2d. %s(%s) imbalance=%.2f",
            i,
            symbol,
            name,
            float(r.imbalance_safe),
        )


# ============================================================
# TOSAMA INAGO
# ============================================================

def log_tosama_inago(df):

    df = _safe_df(df)

    if df.empty:
        return

    if not _safe_symbol(df):
        return

    if "tosama_signal" not in df.columns:
        return

    df = df[df["tosama_signal"] == 1]

    if df.empty:
        return

    logger.info("========== 📊 殿様イナゴ SIGNAL ==========")

    for r in df.itertuples():

        symbol = str(r.symbol)
        name = _symbol_name(symbol)

        logger.info(
            "🐸 %s(%s) Tosama Inago detected",
            symbol,
            name,
        )


# ============================================================
# AI META RANKING
# ============================================================

def log_ai_meta(df, top_n=10):

    df = _safe_df(df)

    if df.empty:
        return

    if not _safe_symbol(df):
        return

    df["ai_meta"] = _safe_ai_score(df)

    rank = (
        df.sort_values("ai_meta", ascending=False)
        .drop_duplicates("symbol")
        .head(top_n)
    )

    if rank.empty:
        return

    logger.info("========== 🤖 AI Meta Ranking ==========")

    for i, r in enumerate(rank.itertuples(), 1):

        symbol = str(r.symbol)
        name = _symbol_name(symbol)

        logger.info(
            "%2d. %s(%s) ai_score=%.2f",
            i,
            symbol,
            name,
            float(r.ai_meta),
        )