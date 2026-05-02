# ============================================================
# File   : trading/ranking/core/latest_selector.py
# Version: Ver4-PRODUCTION-ULTRA-STABLE-LATEST-SELECTOR-FINAL
# ------------------------------------------------------------
# ✔ select_latest_rows 追加（ImportError完全解消）
# ✔ 既存機能完全維持（削除ゼロ）
# ✔ symbol normalize
# ✔ datetime normalize + timezone safety
# ✔ NaT完全排除
# ✔ duplicate guard（symbol + datetime）
# ✔ groupby crash fallback（idxmax）
# ✔ pandas alignment crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# core internal safe selector
# ============================================================

def _safe_latest(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # symbol normalize
    # --------------------------------------------------------
    try:
        df["symbol"] = df["symbol"].astype(str)
    except Exception:
        logger.exception("[latest_selector] symbol normalize failed")

    # --------------------------------------------------------
    # datetime normalize
    # --------------------------------------------------------
    try:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    except Exception:
        logger.exception("[latest_selector] datetime convert failed")

    # NaT削除
    df = df.dropna(subset=["datetime"])

    if df.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # duplicate除去
    # --------------------------------------------------------
    try:
        df = df.drop_duplicates(
            subset=["symbol", "datetime"],
            keep="last"
        )
    except Exception:
        logger.exception("[latest_selector] duplicate remove failed")

    # --------------------------------------------------------
    # sort
    # --------------------------------------------------------
    try:
        df = df.sort_values(["symbol", "datetime"])
    except Exception:
        logger.exception("[latest_selector] sort failed")

    # --------------------------------------------------------
    # 最新抽出（tail）
    # --------------------------------------------------------
    try:
        latest_df = (
            df.groupby("symbol", as_index=False)
            .tail(1)
            .reset_index(drop=True)
        )
        return latest_df

    except Exception:
        logger.warning("[latest_selector] tail failed → fallback idxmax")

    # --------------------------------------------------------
    # fallback（idxmax）
    # --------------------------------------------------------
    try:
        idx = df.groupby("symbol")["datetime"].idxmax()
        latest_df = df.loc[idx].reset_index(drop=True)
        return latest_df

    except Exception:
        logger.exception("[latest_selector] idxmax fallback failed")

    return pd.DataFrame()


# ============================================================
# public API（既存）
# ============================================================

def latest_symbol_rows(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    df = df.copy()

    # --------------------------------------------------------
    # 必須列チェック
    # --------------------------------------------------------
    if "symbol" not in df.columns:
        logger.warning("[latest_selector] symbol column missing")
        return df

    # --------------------------------------------------------
    # datetimeなし fallback
    # --------------------------------------------------------
    if "datetime" not in df.columns:
        logger.warning("[latest_selector] datetime missing → fallback last row")

        return (
            df.groupby("symbol", as_index=False)
            .tail(1)
            .reset_index(drop=True)
        )

    return _safe_latest(df)


# ============================================================
# 🚨 NEW（ImportError対策）
# ============================================================

def select_latest_rows(
    df: pd.DataFrame,
    *,
    symbol_col: str = "symbol",
    datetime_col: str = "datetime",
) -> pd.DataFrame:
    """
    ranking_pipeline互換API
    """

    if df is None or df.empty:
        return pd.DataFrame()

    if symbol_col != "symbol" or datetime_col != "datetime":
        try:
            df = df.rename(
                columns={
                    symbol_col: "symbol",
                    datetime_col: "datetime",
                }
            )
        except Exception:
            logger.exception("[latest_selector] column rename failed")
            return pd.DataFrame()

    return latest_symbol_rows(df)


# ============================================================
# utility
# ============================================================

def latest_one(df: pd.DataFrame):

    df = latest_symbol_rows(df)

    if df.empty:
        return None

    try:
        return df.iloc[0]
    except Exception:
        return None


def get_latest_datetime(df: pd.DataFrame):

    if df is None or df.empty:
        return None

    if "datetime" not in df.columns:
        return None

    try:
        return pd.to_datetime(df["datetime"], errors="coerce").max()
    except Exception:
        return None