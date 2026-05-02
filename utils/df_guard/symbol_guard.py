# ============================================================
# File   : utils/df_guard/symbol_guard.py
# Version: Ver2.0-PRODUCTION-SYMBOL-GUARD-HARDENED
# ------------------------------------------------------------
# ✔ symbol dtype stabilization（str化）
# ✔ suffix除去（.T / .JP / 空白）
# ✔ 数値抽出（7203.T → 7203）
# ✔ float対応（7203.0 → 7203）
# ✔ zero padding（4桁）
# ✔ 列名ゆらぎ吸収（code / ticker 等）
# ✔ 無効symbol除去
# ✔ symbol重複対策（datetime併用）
# ✔ length filter（安全版）
# ✔ 全工程ログ
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# symbol列名の統一
# ============================================================

def normalize_symbol_column(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        aliases = [
            "code",
            "ticker",
            "symbol_code",
            "銘柄コード",
        ]

        if "symbol" not in df.columns:

            for col in aliases:

                if col in df.columns:

                    df = df.rename(columns={col: "symbol"})

                    logger.warning(
                        "[SYMBOL GUARD] alias used: %s -> symbol",
                        col
                    )

                    break

    except Exception as e:

        logger.warning(
            "[SYMBOL GUARD] normalize column failed: %s", e
        )

    return df


# ============================================================
# symbol 正規化（最重要）
# ============================================================

def normalize_symbol_value(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        df = df.copy()

        s = df["symbol"].astype(str)

        # suffix除去
        s = (
            s.str.replace(".T", "", regex=False)
             .str.replace(".JP", "", regex=False)
             .str.replace(" ", "", regex=False)
             .str.strip()
        )

        # float対応（7203.0 → 7203）
        s = s.str.replace(r"\.0$", "", regex=True)

        # 数値抽出（最重要）
        s = s.str.extract(r"(\d{4})", expand=False)

        df["symbol"] = s

    except Exception as e:

        logger.warning(
            "[SYMBOL GUARD] normalize value failed: %s", e
        )

    return df


# ============================================================
# symbol dtype stabilization
# ============================================================

def stabilize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        df = df.copy()

        df["symbol"] = (
            df["symbol"]
            .astype(str)
            .str.strip()
        )

    except Exception as e:

        logger.warning(
            "[SYMBOL GUARD] dtype stabilize failed: %s", e
        )

    return df


# ============================================================
# zero padding（4桁対応）
# ============================================================

def zero_pad_symbol(
    df: pd.DataFrame,
    length: int = 4
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        df = df.copy()

        df["symbol"] = df["symbol"].str.zfill(length)

    except Exception as e:

        logger.warning(
            "[SYMBOL GUARD] zero padding failed: %s", e
        )

    return df


# ============================================================
# remove invalid symbols
# ============================================================

def remove_invalid_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        before = len(df)

        df = df[
            df["symbol"].notna() &
            (df["symbol"] != "") &
            (df["symbol"] != "nan")
        ]

        dropped = before - len(df)

        if dropped > 0:

            logger.warning(
                "[SYMBOL GUARD] invalid symbols removed: %s",
                dropped
            )

    except Exception as e:

        logger.warning(
            "[SYMBOL GUARD] invalid remove failed: %s", e
        )

    return df


# ============================================================
# remove duplicate symbols（datetime考慮）
# ============================================================

def remove_duplicate_symbol(
    df: pd.DataFrame,
    use_datetime: bool = True
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        if use_datetime and "datetime" in df.columns:

            df = (
                df.sort_values(
                    ["symbol", "datetime"],
                    kind="mergesort"
                )
                .drop_duplicates(
                    subset=["symbol", "datetime"],
                    keep="last"
                )
            )

        else:

            df = df.drop_duplicates(
                subset=["symbol"],
                keep="last"
            )

    except Exception as e:

        logger.warning(
            "[SYMBOL GUARD] duplicate remove failed: %s", e
        )

    return df


# ============================================================
# symbol長さチェック（安全版）
# ============================================================

def filter_symbol_length(
    df: pd.DataFrame,
    min_len: int = 4,
    max_len: int = 4
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:

        before = len(df)

        df = df[
            df["symbol"].str.match(r"^\d{4}$", na=False)
        ]

        dropped = before - len(df)

        if dropped > 0:

            logger.warning(
                "[SYMBOL GUARD] filtered invalid format: %s",
                dropped
            )

    except Exception as e:

        logger.warning(
            "[SYMBOL GUARD] length filter failed: %s", e
        )

    return df


# ============================================================
# FULL PIPELINE
# ============================================================

def ensure_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        before = len(df)

        df = normalize_symbol_column(df)

        df = normalize_symbol_value(df)   # ★最重要

        df = stabilize_symbol(df)

        df = zero_pad_symbol(df)

        df = remove_invalid_symbol(df)

        df = filter_symbol_length(df)

        df = remove_duplicate_symbol(df)

        after = len(df)

        """logger.info(
            "[SYMBOL GUARD] rows: %s -> %s",
            before, after
        )"""

    except Exception as e:

        logger.exception(
            "[SYMBOL GUARD] ensure_symbol failed: %s", e
        )

    return df


# ============================================================
# public API
# ============================================================

__all__ = [
    "normalize_symbol_column",
    "normalize_symbol_value",
    "stabilize_symbol",
    "zero_pad_symbol",
    "remove_invalid_symbol",
    "remove_duplicate_symbol",
    "filter_symbol_length",
    "ensure_symbol",
]