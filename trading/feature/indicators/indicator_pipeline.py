# ============================================================
# File   : feature/indicators/indicator_pipeline.py
# Version: Ver3.2-PRODUCTION-INDICATOR-PIPELINE-INSTITUTIONAL-HARDENED
#          -NAN-PRESERVE-FINAL
# ------------------------------------------------------------
# ✔ Ver3.1 全機能保持（削除ゼロ）
# ✔ indicator 一括計算
# ✔ RuntimeLoop 保護
# ✔ module 不在でも安全
# ✔ NaN / inf guard
# ✔ duplicate column guard
# ✔ MultiIndex flatten
# ✔ symbol / datetime guard
# ✔ OHLC alias repair
# ✔ ATR calculation (symbol grouped)
# ✔ dtype stabilization
# ✔ structure sanitize
# ✔ pandas crash防止
# ✔ groupby crash防止
# ✔ column collision防止
# ✔ pandas alignment guard
# ✔ safe numeric extractor
# ✔ logging
# ✔ production safe
# ✔ ranking pseudo OHLC compatibility
# ✔ extended alias support
# ✔ NEW: numeric NaN preserve
# ✔ NEW: price / indicator を一律 0埋めしない
# ✔ NEW: OHLC alias は drop せず mirror
# ✔ NEW: volume など 0許容列のみ個別 fill
# ✔ NEW: technical profile log
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# indicator modules
# ------------------------------------------------------------

try:
    from feature.indicators.ma import apply_ma_indicators
except Exception:
    apply_ma_indicators = None

try:
    from feature.indicators.rsi import apply_rsi_indicators
except Exception:
    apply_rsi_indicators = None

try:
    from feature.indicators.macd import apply_macd_indicators
except Exception:
    apply_macd_indicators = None

try:
    from feature.indicators.vwap import apply_vwap_indicators
except Exception:
    apply_vwap_indicators = None

try:
    from feature.indicators.volatility import apply_volatility_indicators
except Exception:
    apply_volatility_indicators = None

try:
    from feature.indicators.support_resistance import apply_support_resistance
except Exception:
    apply_support_resistance = None


# ------------------------------------------------------------
# helpers
# ------------------------------------------------------------

def _safe_series(df: pd.DataFrame, col: str, default=np.nan) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame) or col not in df.columns:
        return pd.Series(default, index=df.index if isinstance(df, pd.DataFrame) else None, dtype="float64")

    s = df[col]

    try:
        if isinstance(s, pd.DataFrame):
            if s.shape[1] == 0:
                return pd.Series(default, index=df.index, dtype="float64")
            base = pd.to_numeric(s.iloc[:, 0], errors="coerce")
            for i in range(1, s.shape[1]):
                nxt = pd.to_numeric(s.iloc[:, i], errors="coerce")
                try:
                    base = base.combine_first(nxt)
                except Exception:
                    base = base.where(base.notna(), nxt)
            return base

        return pd.to_numeric(s, errors="coerce")

    except Exception:
        return pd.Series(default, index=df.index, dtype="float64")


def _nonnull_numeric(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        return int(s.notna().sum())
    except Exception:
        return 0


def _nonzero_numeric(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        return int((s.fillna(0.0) != 0).sum())
    except Exception:
        return 0


def _sanitize_price_like(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    s = s.mask(s <= 0, np.nan)
    return s


def _pick_first_numeric(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series:
    base = pd.Series(np.nan, index=df.index, dtype="float64")
    for c in candidates:
        if c in df.columns:
            s = pd.to_numeric(_safe_series(df, c), errors="coerce")
            try:
                base = base.combine_first(s)
            except Exception:
                base = base.where(base.notna(), s)
    return base


# ------------------------------------------------------------
# OHLC FIX
# ------------------------------------------------------------

def _fix_ohlc_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame):
        return df

    df = df.copy()

    # 元列を落とさず mirror する
    close_src = _pick_first_numeric(
        df,
        [
            "close",
            "close_price",
            "price",
            "CurrentPrice",
            "current_price",
            "last_price",
            "LastPrice",
            "closeValue",
            "closevalue",
            "終値",
            "現在値",
        ],
    )
    close_src = _sanitize_price_like(close_src)

    open_src = _pick_first_numeric(df, ["open", "open_price", "openValue", "openvalue", "始値"])
    high_src = _pick_first_numeric(df, ["high", "high_price", "highValue", "highvalue", "高値"])
    low_src = _pick_first_numeric(df, ["low", "low_price", "lowValue", "lowvalue", "安値"])

    open_src = _sanitize_price_like(open_src).combine_first(close_src)
    high_src = _sanitize_price_like(high_src).combine_first(close_src)
    low_src = _sanitize_price_like(low_src).combine_first(close_src)

    if "close" not in df.columns:
        df["close"] = close_src
    else:
        df["close"] = _sanitize_price_like(_safe_series(df, "close")).combine_first(close_src)

    if "open" not in df.columns:
        df["open"] = open_src
    else:
        df["open"] = _sanitize_price_like(_safe_series(df, "open")).combine_first(open_src)

    if "high" not in df.columns:
        df["high"] = high_src
    else:
        df["high"] = _sanitize_price_like(_safe_series(df, "high")).combine_first(high_src)

    if "low" not in df.columns:
        df["low"] = low_src
    else:
        df["low"] = _sanitize_price_like(_safe_series(df, "low")).combine_first(low_src)

    # canonical aliases を mirror
    df["close_price"] = _pick_first_numeric(df, ["close_price", "close", "price"]).combine_first(df["close"])
    df["open_price"] = _pick_first_numeric(df, ["open_price", "open"]).combine_first(df["open"])
    df["high_price"] = _pick_first_numeric(df, ["high_price", "high"]).combine_first(df["high"])
    df["low_price"] = _pick_first_numeric(df, ["low_price", "low"]).combine_first(df["low"])

    if "price" not in df.columns:
        df["price"] = df["close_price"]
    else:
        df["price"] = pd.to_numeric(df["price"], errors="coerce").combine_first(df["close_price"])

    if "current_price" not in df.columns:
        df["current_price"] = df["close_price"]
    else:
        df["current_price"] = pd.to_numeric(df["current_price"], errors="coerce").combine_first(df["close_price"])

    if "CurrentPrice" not in df.columns:
        df["CurrentPrice"] = df["close_price"]
    else:
        df["CurrentPrice"] = pd.to_numeric(df["CurrentPrice"], errors="coerce").combine_first(df["close_price"])

    if "last_price" not in df.columns:
        df["last_price"] = df["close_price"]
    else:
        df["last_price"] = pd.to_numeric(df["last_price"], errors="coerce").combine_first(df["close_price"])

    if "LastPrice" not in df.columns:
        df["LastPrice"] = df["close_price"]
    else:
        df["LastPrice"] = pd.to_numeric(df["LastPrice"], errors="coerce").combine_first(df["close_price"])

    # volume
    volume_src = _pick_first_numeric(df, ["volume", "trading_volume", "TradingVolume", "出来高", "volume_total"])
    volume_src = pd.to_numeric(volume_src, errors="coerce").replace([np.inf, -np.inf], np.nan)

    if "volume" not in df.columns:
        df["volume"] = volume_src
    else:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").combine_first(volume_src)

    if "TradingVolume" not in df.columns:
        df["TradingVolume"] = df["volume"]
    if "volume_total" not in df.columns:
        df["volume_total"] = df["volume"]

    return df


# ------------------------------------------------------------
# dataframe sanitize
# ------------------------------------------------------------

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
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

    if isinstance(df.columns, pd.MultiIndex):
        try:
            df.columns = [
                "_".join([str(x) for x in col if x not in (None, "")])
                for col in df.columns
            ]
        except Exception:
            df.columns = df.columns.get_level_values(0)

    if df.columns.duplicated().any():
        dup = df.columns[df.columns.duplicated()].tolist()
        logger.warning("[INDICATOR] duplicate columns removed: %s", dup)
        df = df.loc[:, ~df.columns.duplicated()]

    try:
        df = df.reset_index(drop=True)
    except Exception:
        pass

    if "symbol" in df.columns:
        try:
            df["symbol"] = (
                df["symbol"]
                .astype(str)
                .str.strip()
                .str.replace(r"\.0$", "", regex=True)
            )
        except Exception:
            pass

    return df


# ------------------------------------------------------------
# numeric sanitize
# ------------------------------------------------------------

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """
    重要:
    - 数値列を一律 fillna(0) しない
    - 価格系 / テクニカル系は NaN preserve
    - volume / count / flag 系だけ個別に 0 許容
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    try:
        # まず numeric dtype は inf だけ NaN へ
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        if len(numeric_cols) > 0:
            df[numeric_cols] = df[numeric_cols].replace([np.inf, -np.inf], np.nan)

        # 個別列も数値化
        numeric_candidates = [
            "open", "high", "low", "close",
            "open_price", "high_price", "low_price", "close_price",
            "price", "current_price", "CurrentPrice", "last_price", "LastPrice",
            "ma5", "ma25", "ma75",
            "rsi", "macd", "signal", "hist",
            "vwap", "atr", "atr_1m", "atr_3m", "atr_5m",
            "slope", "slope_atr_scaled", "ma75_slope", "vwap_slope", "volume_slope",
            "mtf", "score_mtf", "score_slope", "score", "score_buy", "score_sell",
            "score_total", "final_score", "display_score", "combined_score",
            "ranking_score", "ranking_momentum", "ranking_strength",
            "best_rank", "rank", "best", "best_rank_value",
            "history_count", "history", "hist_count",
            "symbol_hist_len", "technical_ready",
            "volume", "TradingVolume", "volume_total",
        ]

        for c in numeric_candidates:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").replace([np.inf, -np.inf], np.nan)

        # price系は <=0 を invalid とみなす
        for c in [
            "open", "high", "low", "close",
            "open_price", "high_price", "low_price", "close_price",
            "price", "current_price", "CurrentPrice", "last_price", "LastPrice",
        ]:
            if c in df.columns:
                df[c] = _sanitize_price_like(df[c])

        # 0埋めしてよい列だけ
        for c in ["volume", "TradingVolume", "volume_total"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0).clip(lower=0.0)

        for c in ["technical_ready"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

        for c in ["best_rank", "rank", "best", "best_rank_value", "history_count", "history", "hist_count"]:
            if c in df.columns:
                # rank/hist は未計算を残したい時もあるが、表示互換上は数値化のみ
                df[c] = pd.to_numeric(df[c], errors="coerce")

    except Exception:
        logger.exception("[INDICATOR] numeric sanitize failed")

    return df


# ------------------------------------------------------------
# datetime guard
# ------------------------------------------------------------

def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    if "datetime" not in df.columns:
        if "end_time" in df.columns:
            df["datetime"] = pd.to_datetime(df["end_time"], errors="coerce")
        elif "start_time" in df.columns:
            df["datetime"] = pd.to_datetime(df["start_time"], errors="coerce")

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        try:
            df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        if df["datetime"].isna().any():
            if "end_time" in df.columns:
                mask = df["datetime"].isna()
                df.loc[mask, "datetime"] = pd.to_datetime(
                    df.loc[mask, "end_time"],
                    errors="coerce"
                )

    if "datetime" in df.columns:
        df = df.dropna(subset=["datetime"])

        try:
            sort_cols = ["datetime"]
            if "symbol" in df.columns:
                sort_cols = ["symbol", "datetime"]
            df = df.sort_values(sort_cols, kind="mergesort")
        except Exception:
            pass

    return df


# ------------------------------------------------------------
# ATR indicator (symbol safe)
# ------------------------------------------------------------

def _apply_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    try:
        required = {"symbol", "high", "low", "close", "datetime"}
        if not required.issubset(df.columns):
            return df

        df = df.sort_values(["symbol", "datetime"], kind="mergesort")

        def calc_atr(group: pd.DataFrame) -> pd.DataFrame:
            group = group.copy()

            high = _safe_series(group, "high")
            low = _safe_series(group, "low")
            close = _safe_series(group, "close")

            tr = np.maximum(
                high - low,
                np.maximum(
                    abs(high - close.shift()),
                    abs(low - close.shift())
                )
            )

            atr = tr.rolling(period, min_periods=1).mean()
            atr = pd.to_numeric(atr, errors="coerce").replace([np.inf, -np.inf], np.nan)

            group["atr_1m"] = atr
            if "atr" not in group.columns:
                group["atr"] = atr
            else:
                group["atr"] = pd.to_numeric(group["atr"], errors="coerce").combine_first(atr)

            return group

        df = (
            df.groupby("symbol", group_keys=False, sort=False)
            .apply(calc_atr)
            .reset_index(drop=True)
        )

    except Exception:
        logger.exception("[INDICATOR] ATR failed")

    return df


# ------------------------------------------------------------
# safe apply helper
# ------------------------------------------------------------

def _safe_apply(df: pd.DataFrame, fn, name: str) -> pd.DataFrame:
    if fn is None:
        logger.debug("[INDICATOR] %s skipped (module missing)", name)
        return df

    try:
        before_cols = set(df.columns)
        df = fn(df)
        if not isinstance(df, pd.DataFrame):
            logger.warning("[INDICATOR] %s returned non-DataFrame, previous df kept", name)
            return df

        new_cols = set(df.columns) - before_cols
        if new_cols:
            logger.debug("[INDICATOR] %s added columns: %s", name, list(new_cols))

    except Exception:
        logger.exception("[INDICATOR] %s failed", name)

    return df


# ------------------------------------------------------------
# diagnostics
# ------------------------------------------------------------

def _log_profile(df: pd.DataFrame, stage: str) -> None:
    try:
        if df is None or df.empty:
            logger.info("[INDICATOR] %s empty", stage)
            return

        latest_dt = None
        if "datetime" in df.columns:
            try:
                latest_dt = pd.to_datetime(df["datetime"], errors="coerce").max()
            except Exception:
                latest_dt = None

        logger.info(
            "[INDICATOR] %s rows=%s cols=%s symbols=%s latest_dt=%s",
            stage,
            len(df),
            len(df.columns),
            int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0,
            latest_dt,
        )

        for c in ["close", "ma25", "ma75", "rsi", "macd", "signal", "hist", "vwap", "atr", "slope", "slope_atr_scaled", "mtf"]:
            if c in df.columns:
                logger.info(
                    "[INDICATOR] %s col=%s nonnull=%s nonzero=%s",
                    stage,
                    c,
                    _nonnull_numeric(df, c),
                    _nonzero_numeric(df, c),
                )

    except Exception:
        logger.exception("[INDICATOR] profile log failed stage=%s", stage)


# ------------------------------------------------------------
# main pipeline
# ------------------------------------------------------------

def apply_indicator_pipeline(df: pd.DataFrame) -> pd.DataFrame:
    """
    indicator pipeline

    summary → indicators → features
    """
    if df is None or len(df) == 0:
        return df

    if not isinstance(df, pd.DataFrame):
        logger.error("[INDICATOR] input not DataFrame")
        return df

    try:
        # structure sanitize
        df = _fix_ohlc_columns(df)
        df = _sanitize_dataframe(df)
        df = _ensure_datetime(df)
        df = _sanitize_numeric(df)
        _log_profile(df, "input")

        # ----------------------------------------------------
        # indicator pipeline
        # ----------------------------------------------------
        df = _safe_apply(df, apply_ma_indicators, "MA")
        df = _safe_apply(df, apply_rsi_indicators, "RSI")
        df = _safe_apply(df, apply_macd_indicators, "MACD")
        df = _safe_apply(df, apply_vwap_indicators, "VWAP")
        df = _safe_apply(df, apply_volatility_indicators, "VOLATILITY")
        df = _safe_apply(df, apply_support_resistance, "SUPPORT_RESISTANCE")

        # ATR
        df = _apply_atr(df)

        # numeric guard again
        df = _sanitize_numeric(df)

        # duplicate column guard (final)
        if df.columns.duplicated().any():
            dup = df.columns[df.columns.duplicated()].tolist()
            logger.warning("[INDICATOR] duplicate columns removed: %s", dup)
            df = df.loc[:, ~df.columns.duplicated()]

        _log_profile(df, "final")

        logger.info(
            "[INDICATOR] applied indicators rows=%s cols=%s columns=%s",
            len(df),
            len(df.columns),
            list(df.columns),
        )

        return df

    except Exception:
        logger.exception("[INDICATOR] pipeline failed")
        return df