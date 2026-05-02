# ============================================================
# File   : trading/ranking/summary/bootstrap_technical.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-TECHNICAL
# ------------------------------------------------------------
# 【概要】
#   ranking snapshot 由来 summary に technical 指標を付与
#
# 【計算】
#   ma5 / ma25 / ma75
#   rsi
#   macd / signal / hist
#   atr
#   vwap
#   slope / slope_atr_scaled
#
# 【重要】
#   OHLC はランキング由来方針により close 同値を維持
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from trading.ranking.summary.bootstrap_loader import resolve_callable
from trading.ranking.summary.bootstrap_ohlcv import (
    normalize_datetime,
    normalize_symbol,
    safe_numeric_series,
)

logger = logging.getLogger(__name__)


def fallback_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    close = pd.to_numeric(close, errors="coerce").astype("float64")

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    rsi = rsi.where(~avg_loss.eq(0), 100.0)
    rsi = rsi.where(~avg_gain.eq(0), 0.0)
    rsi = pd.to_numeric(rsi, errors="coerce").fillna(50.0)

    return rsi.clip(lower=0.0, upper=100.0).astype("float64")


def fallback_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = pd.to_numeric(close, errors="coerce").astype("float64")

    ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=1).mean()
    hist = macd - signal

    return (
        pd.to_numeric(macd, errors="coerce").astype("float64"),
        pd.to_numeric(signal, errors="coerce").astype("float64"),
        pd.to_numeric(hist, errors="coerce").astype("float64"),
    )


def fallback_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = safe_numeric_series(df, "high", default=np.nan)
    low = safe_numeric_series(df, "low", default=np.nan)
    close = safe_numeric_series(df, "close", default=np.nan)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
    return pd.to_numeric(atr, errors="coerce").fillna(0.0).astype("float64")


def fallback_vwap(df: pd.DataFrame) -> pd.Series:
    price = safe_numeric_series(df, "close", default=0.0, fill=True)
    volume = safe_numeric_series(df, "volume", default=0.0, fill=True)

    try:
        cum_pv = (price * volume).groupby(df["symbol"]).cumsum()
        cum_v = volume.groupby(df["symbol"]).cumsum()
        vwap = cum_pv / cum_v.replace(0, np.nan)
        return pd.to_numeric(vwap, errors="coerce").fillna(price).astype("float64")
    except Exception:
        logger.exception("[RANKING SUMMARY BOOTSTRAP TECH] VWAP failed")
        return price.astype("float64")


def apply_fallback_technical_indicators(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    x = normalize_symbol(x)
    x = normalize_datetime(x)

    if x.empty:
        return pd.DataFrame()

    x = x.sort_values(["symbol", "datetime"]).copy()

    min_slope = 2
    min_rsi = 5
    min_macd = 5

    out_frames: list[pd.DataFrame] = []

    for symbol, g in x.groupby("symbol", sort=False):
        gg = g.copy()
        gg = gg.sort_values("datetime").copy()

        gg["close"] = safe_numeric_series(gg, "close", default=np.nan)
        gg["volume"] = safe_numeric_series(gg, "volume", default=0.0, fill=True)

        gg["open"] = gg["close"]
        gg["high"] = gg["close"]
        gg["low"] = gg["close"]

        gg["hist_len"] = range(1, len(gg) + 1)

        gg["ma5"] = gg["close"].rolling(5, min_periods=1).mean()
        gg["ma25"] = gg["close"].rolling(25, min_periods=1).mean()
        gg["ma75"] = gg["close"].rolling(75, min_periods=1).mean()

        gg["rsi"] = pd.Series(np.nan, index=gg.index, dtype="float64")
        gg["macd"] = pd.Series(np.nan, index=gg.index, dtype="float64")
        gg["signal"] = pd.Series(np.nan, index=gg.index, dtype="float64")
        gg["hist"] = pd.Series(np.nan, index=gg.index, dtype="float64")

        if len(gg) >= min_rsi:
            try:
                gg["rsi"] = fallback_rsi(gg["close"], period=14)
            except Exception:
                logger.exception("[RANKING SUMMARY BOOTSTRAP TECH] RSI failed symbol=%s", symbol)

        if len(gg) >= min_macd:
            try:
                macd, signal, hist = fallback_macd(gg["close"])
                gg["macd"] = macd
                gg["signal"] = signal
                gg["hist"] = hist
            except Exception:
                logger.exception("[RANKING SUMMARY BOOTSTRAP TECH] MACD failed symbol=%s", symbol)

        gg["atr"] = fallback_atr(gg, period=14)
        gg["vwap"] = fallback_vwap(gg)

        diff = gg["close"].diff()
        atr_safe = gg["atr"].replace(0, np.nan)

        slope_atr = (diff / atr_safe).replace([float("inf"), float("-inf")], np.nan)
        fallback_slope = diff.clip(-5.0, 5.0)

        gg["slope_atr_scaled"] = pd.to_numeric(slope_atr, errors="coerce").fillna(fallback_slope)
        gg["slope"] = pd.to_numeric(gg["slope_atr_scaled"], errors="coerce")

        if len(gg) < min_slope:
            gg["slope_atr_scaled"] = np.nan
            gg["slope"] = np.nan

        gg["technical_ready"] = (
            pd.to_numeric(gg["hist_len"], errors="coerce")
            .fillna(0)
            .ge(min_slope)
            .astype(int)
        )

        out_frames.append(gg)

    if not out_frames:
        return x

    out = pd.concat(out_frames, ignore_index=True)
    out = out.sort_values(["symbol", "datetime"]).copy()

    logger.info(
        "[RANKING SUMMARY BOOTSTRAP TECH] fallback applied interval=%s rows=%d slope_nonnull=%d rsi_nonnull=%d macd_nonnull=%d",
        interval,
        len(out),
        int(pd.to_numeric(out["slope"], errors="coerce").notna().sum()) if "slope" in out.columns else 0,
        int(pd.to_numeric(out["rsi"], errors="coerce").notna().sum()) if "rsi" in out.columns else 0,
        int(pd.to_numeric(out["macd"], errors="coerce").notna().sum()) if "macd" in out.columns else 0,
    )

    return out


def apply_technical_indicators(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    既存 technicals.py があれば優先利用。
    失敗または空なら fallback 計算。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    existing = resolve_callable(
        [
            ("trading.ranking.summary.technicals", "_apply_technical_indicators"),
            ("trading.ranking.summary.technicals", "apply_technical_indicators"),
        ]
    )

    if callable(existing):
        try:
            out = existing(df.copy())
            if isinstance(out, pd.DataFrame) and not out.empty:
                # 既存 technicals.py がOHLCを変更しても、ランキング由来では close 同値に戻す
                if "close" in out.columns:
                    close = pd.to_numeric(out["close"], errors="coerce")
                    out["open"] = close
                    out["high"] = close
                    out["low"] = close
                    out["close"] = close

                logger.info(
                    "[RANKING SUMMARY BOOTSTRAP TECH] existing technicals used interval=%s rows=%d",
                    interval,
                    len(out),
                )
                return out
        except Exception:
            logger.exception(
                "[RANKING SUMMARY BOOTSTRAP TECH] existing technicals failed -> fallback interval=%s",
                interval,
            )

    return apply_fallback_technical_indicators(df, interval=interval)


__all__ = [
    "fallback_rsi",
    "fallback_macd",
    "fallback_atr",
    "fallback_vwap",
    "apply_fallback_technical_indicators",
    "apply_technical_indicators",
]