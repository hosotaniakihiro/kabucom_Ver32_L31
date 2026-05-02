# ============================================================
# File   : trading/summary/engine/processors/resample.py
# Version: Ver32-PRODUCTION-RESAMPLE-PROCESSOR-FINAL
# ------------------------------------------------------------
# ✔ Ver31.1 機能完全保持（削除ゼロ）
# ✔ symbolname欠落自動補完 維持
# ✔ datetime強制変換 維持
# ✔ 必須列ガード強化 維持
# ✔ resample前安全補完 維持
# ✔ duplicate columns 完全防止
# ✔ enhance_guard統合
# ✔ 空DF安全
# ✔ crash防止
# ✔ 非破壊設計
# ✔ CurrentPrice/current_price/LastPrice/TradingVolume 吸収追加
# ✔ resample前に invalid 1分足行を除外
# ✔ resample後に invalid OHLC 行を除外
# ✔ production safe（完全版）
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from trading.summary.resample import resample_1min_to

from trading.summary.engine.guards.enhance_guard import (
    enhance_guard,
    drop_duplicate_columns,
)

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _sanitize_price_like(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    s = s.mask(s <= 0, np.nan)
    return s


def _repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    alias_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "OpenPrice": "open",
        "HighPrice": "high",
        "LowPrice": "low",
        "ClosePrice": "close",
        "price": "close",
        "Price": "close",
        "current_price": "close",
        "CurrentPrice": "close",
        "last_price": "close",
        "LastPrice": "close",
        "trading_volume": "volume",
        "TradingVolume": "volume",
    }

    for src, dst in alias_map.items():
        try:
            if src in out.columns and dst not in out.columns:
                out[dst] = out[src]
            elif src in out.columns and dst in out.columns:
                left = pd.to_numeric(out[dst], errors="coerce")
                right = pd.to_numeric(out[src], errors="coerce")
                out[dst] = left.combine_first(right)
        except Exception:
            logger.debug("[RESAMPLE] alias repair failed src=%s dst=%s", src, dst, exc_info=True)

    if "close" in out.columns:
        close_s = _sanitize_price_like(out["close"])
        for c in ("open", "high", "low"):
            try:
                if c not in out.columns:
                    out[c] = close_s
                else:
                    cur = _sanitize_price_like(out[c])
                    out[c] = cur.combine_first(close_s)
            except Exception:
                logger.debug("[RESAMPLE] OHLC backfill failed col=%s", c, exc_info=True)

    if "volume" not in out.columns:
        for c in ("trading_volume", "TradingVolume"):
            if c in out.columns:
                out["volume"] = out[c]
                break

    return out


def _drop_invalid_ohlc_rows(df: pd.DataFrame, interval: int, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = _repair_ohlc_alias(df.copy())
    if out.empty or "close" not in out.columns:
        return out

    close_s = _sanitize_price_like(out["close"])
    open_s = _sanitize_price_like(out["open"]) if "open" in out.columns else close_s.copy()
    high_s = _sanitize_price_like(out["high"]) if "high" in out.columns else close_s.copy()
    low_s = _sanitize_price_like(out["low"]) if "low" in out.columns else close_s.copy()

    if int(interval) == 1:
        open_s = open_s.combine_first(close_s)
        high_s = high_s.combine_first(close_s)
        low_s = low_s.combine_first(close_s)

    valid = (
        close_s.notna()
        & open_s.notna()
        & high_s.notna()
        & low_s.notna()
        & (high_s >= low_s)
        & (high_s >= open_s)
        & (high_s >= close_s)
        & (low_s <= open_s)
        & (low_s <= close_s)
    )

    before = len(out)
    bad = out.loc[~valid].copy()
    if not bad.empty:
        sample_cols = [
            c for c in [
                "symbol", "symbolname", "datetime",
                "open", "high", "low", "close",
                "open_price", "high_price", "low_price", "close_price",
                "price", "current_price", "CurrentPrice", "last_price", "LastPrice",
                "volume", "trading_volume", "TradingVolume",
            ] if c in bad.columns
        ]
        logger.warning(
            "[RESAMPLE] invalid OHLC removed stage=%s interval=%s removed=%d sample=\n%s",
            stage, interval, len(bad), bad[sample_cols].head(20).to_string(index=False)
        )

    out = out.loc[valid].copy()
    removed = before - len(out)
    if removed > 0:
        logger.warning(
            "[RESAMPLE] invalid OHLC drop stage=%s interval=%s rows=%d->%d",
            stage, interval, before, len(out)
        )

    return out


# ============================================================
# pre sanitize
# ============================================================

def _pre_sanitize(df: pd.DataFrame) -> pd.DataFrame:
    """
    resample前の最終防御
    """

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df = _repair_ohlc_alias(df)

    if "symbol" not in df.columns:
        logger.warning("[RESAMPLE] symbol missing")
        return pd.DataFrame()

    if "datetime" not in df.columns:
        logger.warning("[RESAMPLE] datetime missing")
        return pd.DataFrame()

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    df = df.dropna(subset=["datetime"])

    if df.empty:
        return df

    if "symbolname" not in df.columns:
        logger.warning("[RESAMPLE] symbolname missing → auto fill")
        df["symbolname"] = df["symbol"]

    df = _drop_invalid_ohlc_rows(df, interval=1, stage="pre_resample_source_1m")

    if df.empty:
        return df

    df = df.reset_index(drop=True)

    return df


# ============================================================
# core resample
# ============================================================

def apply_resample(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    resample安全適用

    ✔ interval: 3, 5 など
    ✔ 1min → multi timeframe
    """

    if df is None or df.empty:
        return pd.DataFrame()

    try:
        df = drop_duplicate_columns(df)

        df = _pre_sanitize(df)

        if df.empty:
            return pd.DataFrame()

        df_resampled = resample_1min_to(df, interval)

        if df_resampled is None or df_resampled.empty:
            return pd.DataFrame()

        df_resampled = _repair_ohlc_alias(df_resampled)
        df_resampled = _drop_invalid_ohlc_rows(df_resampled, interval=interval, stage="post_resample")

        if df_resampled.empty:
            return pd.DataFrame()

        df_resampled = drop_duplicate_columns(df_resampled)
        df_resampled = enhance_guard(df_resampled)

        if df_resampled is None or df_resampled.empty:
            return pd.DataFrame()

        return df_resampled

    except Exception:
        logger.exception(
            "[RESAMPLE PROCESSOR] failed interval=%s",
            interval
        )
        return pd.DataFrame()


# ============================================================
# strict version
# ============================================================

def apply_resample_strict(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        before_rows = len(df)
        df_out = apply_resample(df, interval)
        after_rows = len(df_out)

        logger.info(
            "[RESAMPLE PROCESSOR] interval=%s rows: %s -> %s",
            interval,
            before_rows,
            after_rows
        )

        return df_out

    except Exception:
        logger.exception(
            "[RESAMPLE PROCESSOR STRICT] failed interval=%s",
            interval
        )
        return pd.DataFrame()


# ============================================================
# safe wrapper
# ============================================================

def safe_resample(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    return apply_resample(df, interval)


# ============================================================
# multi-resample helper
# ============================================================

def apply_multi_resample(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if df is None or df.empty:
        return {
            "3min": pd.DataFrame(),
            "5min": pd.DataFrame(),
        }

    try:
        df_3m = apply_resample(df, 3)
        df_5m = apply_resample(df, 5)

        return {
            "3min": df_3m,
            "5min": df_5m,
        }

    except Exception:
        logger.exception("[RESAMPLE PROCESSOR] multi failed")

        return {
            "3min": pd.DataFrame(),
            "5min": pd.DataFrame(),
        }


# ============================================================
# public API
# ============================================================

__all__ = [
    "apply_resample",
    "apply_resample_strict",
    "apply_multi_resample",
    "safe_resample",
]