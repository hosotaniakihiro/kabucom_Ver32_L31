# ============================================================
# File   : utils/normalize.py
# Version: Ver33-PRODUCTION-NORMALIZER-FULL-SYMBOLSAFE-NONDESTRUCTIVE-OHLC
# ------------------------------------------------------------
# ✔ Ver32 完全互換ベース
# ✔ symbol完全正規化（.T / .JP / float / 文字混入対応）
# ✔ 4桁数字 / 3桁+英字 / 5桁数字 を許容
# ✔ datetime完全正規化（timezone / NaT防止）
# ✔ price alias統一
# ✔ volume alias統一
# ✔ dtype安全化（object列も対象）
# ✔ NaN / inf 防御
# ✔ DataFrame crash防止
# ✔ NEW: OHLC 非破壊補完
# ✔ NEW: 価格0/負値を無効扱い
# ✔ NEW: loader済み既存OHLCを壊さない
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# internal helpers
# ============================================================

def _safe_numeric_series(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(np.nan, index=df.index if isinstance(df, pd.DataFrame) else None, dtype="float64")
    s = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    return s


def _safe_price_series(df: pd.DataFrame, col: str) -> pd.Series:
    s = _safe_numeric_series(df, col)
    return s.mask(s <= 0, np.nan)


# ============================================================
# symbol normalize（最重要）
# ============================================================

def normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:
        df = df.copy()

        s = df["symbol"].astype(str)

        s = (
            s.str.replace(".T", "", regex=False)
             .str.replace(".JP", "", regex=False)
             .str.replace(" ", "", regex=False)
             .str.strip()
             .str.upper()
        )

        s = s.str.replace(r"\.0$", "", regex=True)
        s = s.str.extract(r"([0-9A-Z]+)", expand=False)

        valid_mask = s.str.match(r"^(?:\d{4}|\d{3}[A-Z]|\d{5})$", na=False)

        before = len(df)

        df["symbol"] = s.where(valid_mask, pd.NA)
        df = df[df["symbol"].notna()].copy()

        after = len(df)

        if before != after:
            logger.warning("[NORMALIZE] symbol filtered: %s", before - after)

    except Exception as e:
        logger.exception("[NORMALIZE] symbol normalize failed: %s", e)

    return df


# ============================================================
# datetime normalize
# ============================================================

def normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    try:
        df = df.copy()

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        try:
            if getattr(df["datetime"].dt, "tz", None) is not None:
                try:
                    df["datetime"] = df["datetime"].dt.tz_convert(None)
                except Exception:
                    df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        before = len(df)
        df = df[df["datetime"].notna()].copy()
        after = len(df)

        if before != after:
            logger.warning("[NORMALIZE] datetime NaT removed: %s", before - after)

    except Exception as e:
        logger.exception("[NORMALIZE] datetime normalize failed: %s", e)

    return df


# ============================================================
# price alias repair
# ============================================================

def normalize_price(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        if "price" not in df.columns:
            aliases = [
                "close",
                "close_price",
                "last",
                "last_price",
                "CurrentPrice",
                "current_price",
            ]

            for col in aliases:
                if col in df.columns:
                    df["price"] = df[col]
                    logger.warning("[NORMALIZE] price alias used: %s", col)
                    break
        else:
            # price既存なら sanitize のみ
            df["price"] = _safe_price_series(df, "price")

    except Exception as e:
        logger.exception("[NORMALIZE] price normalize failed: %s", e)

    return df


# ============================================================
# volume alias repair
# ============================================================

def normalize_volume(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        if "volume" not in df.columns:
            aliases = [
                "vol",
                "Volume",
                "turnover",
                "trading_volume",
            ]

            for col in aliases:
                if col in df.columns:
                    df["volume"] = df[col]
                    logger.warning("[NORMALIZE] volume alias used: %s", col)
                    break

        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0)

    except Exception as e:
        logger.exception("[NORMALIZE] volume normalize failed: %s", e)

    return df


# ============================================================
# OHLC 非破壊保証
# ============================================================

def ensure_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """
    非破壊方針:
    - 既に存在する OHLC を尊重
    - 0 / 負値は価格として無効
    - close を基準に open/high/low を補完するのは「列が完全欠損」の場合だけ
    - 既存OHLCがある場合は勝手に上書きしない
    """
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        # ----------------------------------------------------
        # close を最優先で確定
        # ----------------------------------------------------
        if "close" not in df.columns:
            for c in ["close_price", "price", "last", "last_price", "CurrentPrice", "current_price"]:
                if c in df.columns:
                    df["close"] = df[c]
                    break

        if "close" in df.columns:
            df["close"] = _safe_price_series(df, "close")
        else:
            return df

        # price は close の別名としてのみ補完
        if "price" not in df.columns and "close" in df.columns:
            df["price"] = df["close"]
        elif "price" in df.columns:
            price_s = _safe_price_series(df, "price")
            df["price"] = price_s.combine_first(df["close"])

        # ----------------------------------------------------
        # open/high/low の候補解決
        # ----------------------------------------------------
        alias_map = {
            "open": ["open", "open_price", "opening_price"],
            "high": ["high", "high_price"],
            "low": ["low", "low_price"],
        }

        for logical, aliases in alias_map.items():
            base = pd.Series(np.nan, index=df.index, dtype="float64")

            for c in aliases:
                if c in df.columns:
                    s = _safe_price_series(df, c)
                    base = base.combine_first(s)

            # 値が全く無い場合だけ close で最小補完
            df[logical] = base.combine_first(df["close"])

        # ----------------------------------------------------
        # backward aliases
        # ----------------------------------------------------
        for alias, src in {
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
            "last_price": "close",
            "current_price": "close",
        }.items():
            if alias not in df.columns and src in df.columns:
                df[alias] = df[src]
            elif alias in df.columns and src in df.columns:
                alias_s = _safe_price_series(df, alias)
                src_s = _safe_price_series(df, src)
                df[alias] = alias_s.combine_first(src_s)

        # ----------------------------------------------------
        # invalid OHLC 可視化
        # ----------------------------------------------------
        open_s = _safe_price_series(df, "open")
        high_s = _safe_price_series(df, "high")
        low_s = _safe_price_series(df, "low")
        close_s = _safe_price_series(df, "close")

        invalid = (
            open_s.isna() | high_s.isna() | low_s.isna() | close_s.isna()
            | (high_s < low_s)
            | (high_s < open_s)
            | (high_s < close_s)
            | (low_s > open_s)
            | (low_s > close_s)
        )

        invalid_count = int(invalid.fillna(False).sum())
        if invalid_count > 0:
            logger.warning("[NORMALIZE] invalid OHLC rows detected: %d", invalid_count)

    except Exception as e:
        logger.exception("[NORMALIZE] OHLC ensure failed: %s", e)

    return df


# ============================================================
# 数値安全化
# ============================================================

def normalize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        num_cols = list(df.select_dtypes(include=[np.number]).columns)

        critical_numeric_cols = [
            "open", "high", "low", "close",
            "open_price", "high_price", "low_price", "close_price",
            "price", "volume", "last", "last_price",
            "CurrentPrice", "current_price",
        ]

        target_cols = []
        seen = set()

        for col in num_cols + critical_numeric_cols:
            if col in df.columns and col not in seen:
                target_cols.append(col)
                seen.add(col)

        for col in target_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)

        # 価格系は 0/負値を無効扱い
        for col in [
            "open", "high", "low", "close",
            "open_price", "high_price", "low_price", "close_price",
            "price", "last", "last_price", "CurrentPrice", "current_price",
        ]:
            if col in df.columns:
                df[col] = _safe_price_series(df, col)

        # volume は 0 を許容
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0)

    except Exception as e:
        logger.exception("[NORMALIZE] numeric normalize failed: %s", e)

    return df


# ============================================================
# フルパイプライン
# ============================================================

def normalize_all(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    if df.empty:
        return df

    try:
        df = normalize_symbol(df)
        df = normalize_datetime(df)
        df = normalize_price(df)
        df = normalize_volume(df)
        df = ensure_ohlc(df)
        df = normalize_numeric(df)

    except Exception as e:
        logger.exception("[NORMALIZE] normalize_all failed: %s", e)

    return df


# ============================================================
# public API
# ============================================================

__all__ = [
    "normalize_symbol",
    "normalize_datetime",
    "normalize_price",
    "normalize_volume",
    "ensure_ohlc",
    "normalize_numeric",
    "normalize_all",
]