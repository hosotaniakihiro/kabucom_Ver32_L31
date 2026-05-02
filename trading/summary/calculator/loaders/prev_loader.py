# ============================================================
# File   : trading/summary/calculator/loaders/prev_loader.py
# Version: Ver3.0-PRODUCTION-PREV-LOADER-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ 過去1分足ロード（DB正本）
# ✔ NAS（UNC path）耐性
# ✔ import安全化（循環回避）
# ✔ symbol dtype完全統一
# ✔ datetime完全復元
# ✔ duplicate完全防止
# ✔ bars制限安定化
# ✔ numeric完全防御
# ✔ MultiIndex防御
# ✔ crash isolation
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import pandas as pd
import numpy as np
from typing import List

logger = logging.getLogger(__name__)


# ============================================================
# safe import（循環防止）
# ============================================================

def _safe_import_loader():
    try:
        from trading.summary.persistence.summary_loader import (
            load_prev_1min_summary_all,
        )
        return load_prev_1min_summary_all
    except Exception:
        logger.exception("[PREV LOADER] loader import failed")
        return None

# ============================================================
# dataframe sanitize
# ============================================================

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # MultiIndex flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # duplicate columns 제거
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

    return df


# ============================================================
# datetime normalize
# ============================================================

def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:
        logger.warning("[PREV LOADER] datetime missing")
        return pd.DataFrame()

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    before = len(df)

    df = df.dropna(subset=["datetime"])

    dropped = before - len(df)

    if dropped > 0:
        logger.warning(
            "[PREV LOADER] dropped invalid datetime rows=%s",
            dropped
        )

    return df


# ============================================================
# numeric sanitize
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    num_cols = df.select_dtypes(include="number").columns

    if len(num_cols) > 0:

        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
            .astype("float64")
        )

    return df


# ============================================================
# duplicate guard
# ============================================================

def _deduplicate(df: pd.DataFrame) -> pd.DataFrame:

    if {"symbol", "datetime"}.issubset(df.columns):

        df = (
            df
            .sort_values(["symbol", "datetime"])
            .drop_duplicates(["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )

    return df


# ============================================================
# bars limiter（安定版）
# ============================================================

def _limit_bars(df: pd.DataFrame, bars: int) -> pd.DataFrame:

    try:

        df = df.sort_values("datetime")

        df = (
            df
            .groupby("symbol", as_index=False, sort=False)
            .tail(bars)
            .reset_index(drop=True)
        )

    except Exception:
        logger.exception("[PREV LOADER] bars limit failed")

    return df


# ============================================================
# main loader
# ============================================================

def load_prev_summary(
    symbols: List[str] | None,
    end_time: dt.datetime,
    *,
    bars: int = 80,
    max_trade_days: int = 30,
) -> pd.DataFrame:

    """
    過去1分足ロード（完全安定版）
    """

    if symbols is None or len(symbols) == 0:
        return pd.DataFrame()

    try:

        # ----------------------------------------------------
        # normalize symbols
        # ----------------------------------------------------

        symbols = [str(s) for s in symbols]

        # ----------------------------------------------------
        # loader取得（安全）
        # ----------------------------------------------------

        loader = _safe_import_loader()

        if loader is None:
            return pd.DataFrame()

        # ----------------------------------------------------
        # DB load（NAS耐性）
        # ----------------------------------------------------

        try:

            df = loader(
                symbols=symbols,
                end_time=end_time,
                bars=bars,
                max_trade_days=max_trade_days,
            )

        except Exception:

            logger.exception("[PREV LOADER] DB load failed")

            return pd.DataFrame()

        if df is None or df.empty:
            return pd.DataFrame()

        # ----------------------------------------------------
        # sanitize
        # ----------------------------------------------------

        df = _sanitize_dataframe(df)

        # symbol dtype
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str)

        # datetime normalize
        df = _normalize_datetime(df)

        if df.empty:
            return df

        # ----------------------------------------------------
        # duplicate guard
        # ----------------------------------------------------

        df = _deduplicate(df)

        # ----------------------------------------------------
        # bars制限
        # ----------------------------------------------------

        df = _limit_bars(df, bars)

        # ----------------------------------------------------
        # numeric sanitize
        # ----------------------------------------------------

        df = _sanitize_numeric(df)

        return df

    except Exception:

        logger.exception("[PREV LOADER] fatal error")

        return pd.DataFrame()


# ============================================================
# debug loader
# ============================================================

def load_prev_debug(symbols, end_time):

    df = load_prev_summary(symbols, end_time)

    try:

        logger.info(
            "[PREV DEBUG] rows=%s symbols=%s",
            len(df),
            len(df["symbol"].unique()) if "symbol" in df.columns else 0
        )

    except Exception:
        pass

    return df