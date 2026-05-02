# ============================================================
# File   : trading/summary/pipeline/indicator_enrich.py
# Version: Ver32_L05-SPLIT-INDICATOR-ENRICH
# Purpose:
#   PUSH/Yahoo stock_summary 用 indicator 補完
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .dataframe_safe import (
    ensure_dataframe,
    ensure_primary_datetime_col,
    safe_latest_dt,
    safe_non_null,
    safe_non_zero,
    safe_symbols,
)
from .trade_universe_filter import apply_pipeline_common_trade_universe_filter

logger = logging.getLogger(__name__)

try:
    from trading.summary.indicators.atr_slope_safe import add_atr_and_slope_safe
except Exception:
    add_atr_and_slope_safe = None

INDICATOR_COLS = [
    "rsi",
    "macd",
    "signal",
    "atr",
    "slope",
    "slope_atr_scaled",
    "score",
    "score_buy",
    "score_sell",
    "score_slope",
    "score_mtf",
    "score_total",
    "final_score",
    "display_score",
]


def has_ohlc(df: pd.DataFrame) -> bool:
    return all(c in df.columns for c in ("open", "high", "low", "close"))


def has_symbol_dt(df: pd.DataFrame) -> bool:
    return "symbol" in df.columns and any(
        c in df.columns
        for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time")
    )


def coerce_ohlc_numeric(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    alias_pairs = {
        "open": ["open_price"],
        "high": ["high_price"],
        "low": ["low_price"],
        "close": ["close_price", "current_price", "price"],
    }

    for target, aliases in alias_pairs.items():
        if target not in out.columns:
            for alias in aliases:
                if alias in out.columns:
                    out[target] = out[alias]
                    break

    for c in ("open", "high", "low", "close", "volume", "turnover", "vwap"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    return out


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    ema12 = series.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = series.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    return macd, signal


def log_indicator_profile(tag: str, df: pd.DataFrame) -> None:
    try:
        logger.info(
            "[summary_pipeline][%s] rows=%s symbols=%s latest_dt=%s cols=%s",
            tag,
            len(df),
            safe_symbols(df),
            safe_latest_dt(df),
            list(df.columns),
        )

        for c in INDICATOR_COLS:
            if c not in df.columns:
                logger.warning("[summary_pipeline][%s] missing indicator col=%s", tag, c)
            else:
                logger.info(
                    "[summary_pipeline][%s] %s non_null=%s non_zero=%s",
                    tag,
                    c,
                    safe_non_null(df, c),
                    safe_non_zero(df, c),
                )
    except Exception:
        pass


def should_enrich_indicators(df: pd.DataFrame) -> bool:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False
    if not has_ohlc(df):
        return False
    if not has_symbol_dt(df):
        return False

    missing = [
        c
        for c in ("rsi", "macd", "signal", "atr", "slope", "slope_atr_scaled")
        if c not in df.columns
    ]
    if missing:
        return True

    non_null_total = sum(
        safe_non_null(df, c)
        for c in ("rsi", "macd", "signal", "atr", "slope", "slope_atr_scaled")
    )
    return non_null_total == 0


def ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for c in ("rsi", "macd", "signal", "slope", "slope_atr_scaled"):
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    out["base_score"] = 0.0
    out.loc[out["rsi"] > 55, "base_score"] += 10.0
    out.loc[out["rsi"] > 65, "base_score"] += 10.0
    out.loc[out["macd"] > out["signal"], "base_score"] += 10.0

    out["trend_score"] = 0.0
    out.loc[out["slope"] > 0, "trend_score"] += 10.0
    out.loc[out["slope_atr_scaled"] > 0.02, "trend_score"] += 10.0

    out["momentum_score"] = 0.0
    out.loc[out["macd"] > 0, "momentum_score"] += 10.0
    out.loc[out["rsi"] > 50, "momentum_score"] += 5.0

    out["velocity_score"] = 0.0
    out.loc[out["slope_atr_scaled"] > 0, "velocity_score"] += 10.0
    out.loc[out["slope_atr_scaled"] > 0.02, "velocity_score"] += 10.0

    out["penalty_score"] = 0.0
    out.loc[out["rsi"] >= 85, "penalty_score"] += 5.0
    out.loc[pd.to_numeric(out.get("close", 0), errors="coerce").fillna(0) <= 0, "penalty_score"] += 20.0

    if "score_mtf" not in out.columns:
        out["score_mtf"] = 0.0

    out["score_buy"] = (
        out["base_score"]
        + out["trend_score"]
        + out["momentum_score"]
        + out["velocity_score"]
    )
    out["score_sell"] = out["penalty_score"]
    out["score_slope"] = out["trend_score"]
    out["score_total"] = out["score_buy"] - out["score_sell"]
    out["score"] = out["score_total"]
    out["final_score"] = out["score_total"] + pd.to_numeric(out["score_mtf"], errors="coerce").fillna(0.0)
    out["display_score"] = out["final_score"]

    return out


def force_enrich_indicators(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    OHLCだけで返ってきた場合に、最低限の指標とscoreを補完する。

    ATR/slopeは trading.summary.indicators.atr_slope_safe を優先する。
    ranking_summary には使用しない。
    """
    out = ensure_dataframe(df, "force_enrich_input")
    if out.empty:
        return out

    out = ensure_primary_datetime_col(out)
    out = coerce_ohlc_numeric(out)

    if not has_ohlc(out):
        logger.info(
            "[summary_pipeline] force_enrich skipped interval=%s reason=no_ohlc cols=%s",
            interval,
            list(out.columns),
        )
        return out

    if "symbol" not in out.columns or "datetime" not in out.columns:
        logger.info(
            "[summary_pipeline] force_enrich skipped interval=%s reason=no_symbol_or_datetime cols=%s",
            interval,
            list(out.columns),
        )
        return out

    out["symbol"] = out["symbol"].astype(str)
    out = out.dropna(subset=["symbol", "datetime", "open", "high", "low", "close"]).copy()

    if out.empty:
        return out

    out = apply_pipeline_common_trade_universe_filter(
        out,
        interval=interval,
        context="force_enrich_before_indicator",
    )

    if out.empty:
        return out

    out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

    parts = []
    for symbol, one in out.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime").reset_index(drop=True)
        close = pd.to_numeric(one["close"], errors="coerce")

        if "rsi" not in one.columns or safe_non_null(one, "rsi") == 0:
            one["rsi"] = calc_rsi(close, period=14)

        if "macd" not in one.columns or "signal" not in one.columns:
            one["macd"], one["signal"] = calc_macd(close)
        elif safe_non_null(one, "macd") == 0:
            one["macd"], one["signal"] = calc_macd(close)

        parts.append(one)

    enriched = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if enriched.empty:
        return enriched

    if add_atr_and_slope_safe is not None:
        try:
            enriched = add_atr_and_slope_safe(
                enriched,
                atr_period=14,
                slope_period=5,
                price_col="close",
                high_col="high",
                low_col="low",
                group_col="symbol",
                datetime_col="datetime",
                overwrite=True,
            )
        except Exception as e:
            logger.error(
                "[summary_pipeline] add_atr_and_slope_safe failed interval=%s err=%s: %s",
                interval,
                type(e).__name__,
                str(e)[:300],
                exc_info=False,
            )
    else:
        logger.warning(
            "[summary_pipeline] add_atr_and_slope_safe unavailable; atr/slope columns may remain zero"
        )

    enriched = ensure_score_columns(enriched)
    log_indicator_profile(f"FORCE-ENRICH-{interval}m", enriched)

    return enriched.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)


__all__ = [
    "INDICATOR_COLS",
    "should_enrich_indicators",
    "force_enrich_indicators",
    "log_indicator_profile",
]
