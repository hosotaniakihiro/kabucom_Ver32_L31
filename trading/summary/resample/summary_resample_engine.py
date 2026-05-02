# ============================================================
# File   : trading/summary/resample/summary_resample_engine.py
# Version: Ver12-INSTITUTIONAL-RESAMPLE-ENGINE
# ------------------------------------------------------------
# ✔ 1min → Nmin 安定 resample
# ✔ OHLC 完全保証
# ✔ symbol別 resample
# ✔ datetime 秒ズレ除去
# ✔ duplicate bar防止
# ✔ pandas crash防止
# ✔ massive dataframe対応
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# SAFE NUMERIC
# ============================================================

def _safe_numeric(df: pd.DataFrame):

    for col in ["open", "high", "low", "close", "volume", "vwap"]:

        if col in df.columns:

            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )


# ============================================================
# OHLC REPAIR
# ============================================================

def _repair_ohlc(df: pd.DataFrame):

    if "close" not in df.columns:
        return df

    if "open" not in df.columns:
        df["open"] = df["close"]

    if "high" not in df.columns:
        df["high"] = df["close"]

    if "low" not in df.columns:
        df["low"] = df["close"]

    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)

    return df


# ============================================================
# RESAMPLE CORE
# ============================================================

def _resample_symbol(df: pd.DataFrame, interval: int):

    if df.empty:
        return df

    df = df.copy()

    df = df.sort_values("datetime")

    df = df.set_index("datetime")

    rule = f"{interval}T"

    agg = {}

    if "open" in df.columns:
        agg["open"] = "first"

    if "high" in df.columns:
        agg["high"] = "max"

    if "low" in df.columns:
        agg["low"] = "min"

    if "close" in df.columns:
        agg["close"] = "last"

    if "volume" in df.columns:
        agg["volume"] = "sum"

    if "vwap" in df.columns:
        agg["vwap"] = "mean"

    out = df.resample(rule).agg(agg)

    out = out.dropna(subset=["close"], how="all")

    out = out.reset_index()

    return out


# ============================================================
# MAIN ENGINE
# ============================================================

def resample_1min_to(df: pd.DataFrame, interval: int):

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # ensure datetime
        # ----------------------------------------------------

        if "datetime" not in df.columns:

            if isinstance(df.index, pd.DatetimeIndex):
                df["datetime"] = df.index
            else:
                raise RuntimeError("datetime missing")

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        df = df.dropna(subset=["datetime"])

        # 秒ズレ除去
        df["datetime"] = df["datetime"].dt.floor("min")

        # ----------------------------------------------------
        # ensure symbol
        # ----------------------------------------------------

        if "symbol" not in df.columns:

            logger.error("[RESAMPLE] symbol missing")

            return df

        df["symbol"] = df["symbol"].astype(str)

        # ----------------------------------------------------
        # numeric normalize
        # ----------------------------------------------------

        _safe_numeric(df)

        df = _repair_ohlc(df)

        # ----------------------------------------------------
        # groupby symbol
        # ----------------------------------------------------

        result = []

        for symbol, g in df.groupby("symbol", sort=False):

            try:

                out = _resample_symbol(g, interval)

                if out.empty:
                    continue

                out["symbol"] = symbol

                result.append(out)

            except Exception:

                logger.exception(
                    "[RESAMPLE] symbol failed %s",
                    symbol
                )

        if not result:
            return pd.DataFrame()

        out = pd.concat(result, ignore_index=True)

        # ----------------------------------------------------
        # duplicate protection
        # ----------------------------------------------------

        out = out.drop_duplicates(
            subset=["symbol", "datetime"],
            keep="last"
        )

        out = out.sort_values(
            ["symbol", "datetime"]
        )

        return out

    except Exception:

        logger.exception("[RESAMPLE] engine failed")

        return df