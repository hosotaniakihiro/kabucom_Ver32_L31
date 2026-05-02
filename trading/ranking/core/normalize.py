# ============================================================
# File   : trading/ranking/core/normalize.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-NORMALIZE
# ------------------------------------------------------------
# ✔ symbol normalize（型・空白・ゼロ埋め対応）
# ✔ datetime normalize（NaT除去・timezone統一）
# ✔ column name normalize（lower統一）
# ✔ dtype stabilization
# ✔ safe fallback
# ✔ pandas crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# public API
# ============================================================

def normalize_all(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.copy()

    df = normalize_columns(df)
    df = normalize_symbol(df)
    df = normalize_datetime(df)
    df = normalize_numeric_dtype(df)

    return df


# ============================================================
# column normalize
# ============================================================

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df.columns = [
            str(c).strip().lower()
            for c in df.columns
        ]

    except Exception:
        logger.exception("column normalize failed")

    return df


# ============================================================
# symbol normalize
# ============================================================

def normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    try:

        df["symbol"] = (
            df["symbol"]
            .astype(str)
            .str.strip()
            .str.replace(".T", "", regex=False)
        )

        # 数値だけならゼロ埋め（日本株4桁）
        mask = df["symbol"].str.match(r"^\d+$")

        df.loc[mask, "symbol"] = (
            df.loc[mask, "symbol"]
            .str.zfill(4)
        )

    except Exception:
        logger.exception("symbol normalize failed")

    return df


# ============================================================
# datetime normalize
# ============================================================

def normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:
        return df

    try:

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        # timezone 제거（統一）
        if hasattr(df["datetime"].dt, "tz"):
            try:
                df["datetime"] = df["datetime"].dt.tz_localize(None)
            except Exception:
                pass

        # NaT削除
        bad = df["datetime"].isna().sum()

        if bad > 0:

            logger.warning(
                "[normalize] drop NaT datetime -> %s",
                bad
            )

            df = df.dropna(subset=["datetime"])

    except Exception:
        logger.exception("datetime normalize failed")

    return df


# ============================================================
# numeric dtype normalize
# ============================================================

def normalize_numeric_dtype(df: pd.DataFrame) -> pd.DataFrame:

    try:

        for col in df.columns:

            if df[col].dtype == "object":

                # 数値変換可能なら変換
                converted = pd.to_numeric(
                    df[col],
                    errors="ignore"
                )

                df[col] = converted

        # inf / nan 対応
        num_cols = df.select_dtypes(include=np.number).columns

        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

    except Exception:
        logger.exception("numeric dtype normalize failed")

    return df


# ============================================================
# backward compatible APIs（既存コード用）
# ============================================================

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_all(df)