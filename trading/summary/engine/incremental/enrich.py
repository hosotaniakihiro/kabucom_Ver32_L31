# ============================================================
# File   : trading/summary/engine/incremental/enrich.py
# Version: Ver1.0-INCREMENTAL-ENRICH
# ------------------------------------------------------------
# ✔ indicator rescue
# ✔ RSI / MACD / ATR / slope の強制補完
# ✔ indicator profile logging
# ✔ safe_indicator/safe_mtf/safe_scoring後の救済責務を分離
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_INDICATOR_COLS = [
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


def ensure_df(df: pd.DataFrame | None) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df.copy()
    return pd.DataFrame()


def to_numeric(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time"):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce")
            try:
                out[c] = out[c].dt.tz_localize(None)
            except Exception:
                pass
    if "datetime" not in out.columns:
        for c in ("dt", "timestamp", "end_time", "snapshot_time"):
            if c in out.columns:
                out["datetime"] = pd.to_datetime(out[c], errors="coerce")
                try:
                    out["datetime"] = out["datetime"].dt.tz_localize(None)
                except Exception:
                    pass
                break
    return out


def sort_symbol_dt(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_df(df)
    if out.empty:
        return out
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str)
    if "symbol" in out.columns and "datetime" in out.columns:
        out = out.sort_values(["symbol", "datetime"], kind="stable")
    return out.reset_index(drop=True)


def safe_non_null(df: pd.DataFrame, col: str) -> int:
    try:
        if col in df.columns:
            return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        pass
    return 0


def safe_non_zero(df: pd.DataFrame, col: str) -> int:
    try:
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            return int((s.fillna(0) != 0).sum())
    except Exception:
        pass
    return 0


def log_indicator_profile(tag: str, df: pd.DataFrame) -> None:
    try:
        if df is None or df.empty:
            logger.warning("[INCREMENTAL SUMMARY][%s] empty", tag)
            return

        logger.info(
            "[INCREMENTAL SUMMARY][%s] rows=%s symbols=%s cols=%s",
            tag,
            len(df),
            df["symbol"].astype(str).nunique() if "symbol" in df.columns else 0,
            list(df.columns),
        )

        for c in _INDICATOR_COLS:
            if c not in df.columns:
                logger.warning(
                    "[INCREMENTAL SUMMARY][%s] missing indicator col=%s",
                    tag,
                    c,
                )
            else:
                logger.info(
                    "[INCREMENTAL SUMMARY][%s] %s non_null=%s non_zero=%s",
                    tag,
                    c,
                    safe_non_null(df, c),
                    safe_non_zero(df, c),
                )
    except Exception:
        logger.exception("[INCREMENTAL SUMMARY] log_indicator_profile failed tag=%s", tag)


def has_ohlc(df: pd.DataFrame) -> bool:
    return all(c in df.columns for c in ("open", "high", "low", "close"))


def needs_indicator_rescue(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return False
    if not has_ohlc(df):
        return False
    if "symbol" not in df.columns or "datetime" not in df.columns:
        return False

    missing = [c for c in ("rsi", "macd", "signal", "atr", "slope") if c not in df.columns]
    if missing:
        return True

    total_non_null = sum(safe_non_null(df, c) for c in ("rsi", "macd", "signal", "atr", "slope"))
    return total_non_null == 0


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


def calc_atr(one: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = one["close"].shift(1)
    tr1 = one["high"] - one["low"]
    tr2 = (one["high"] - prev_close).abs()
    tr3 = (one["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def calc_slope(series: pd.Series, period: int = 5) -> pd.Series:
    return (series - series.shift(period)) / series.shift(period).replace(0, np.nan)


def force_enrich_indicators(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = ensure_df(df)
    if out.empty:
        return out
    if not has_ohlc(out):
        return out

    out = ensure_datetime(out)
    out = to_numeric(out, ["open", "high", "low", "close", "volume", "turnover", "vwap"])
    out = out.dropna(subset=["datetime"]).copy()

    if "symbol" not in out.columns or out.empty:
        return out

    out["symbol"] = out["symbol"].astype(str)
    out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

    parts = []
    for symbol, one in out.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime").reset_index(drop=True)

        close = pd.to_numeric(one["close"], errors="coerce")
        one["rsi"] = calc_rsi(close, period=14)
        one["macd"], one["signal"] = calc_macd(close)
        one["atr"] = calc_atr(one, period=14)
        one["slope"] = calc_slope(close, period=5)
        one["slope_atr_scaled"] = one["slope"] / one["atr"].replace(0, np.nan)

        one["base_score"] = 0.0
        one.loc[one["rsi"] > 55, "base_score"] += 10.0
        one.loc[one["rsi"] > 65, "base_score"] += 10.0
        one.loc[one["macd"] > one["signal"], "base_score"] += 10.0

        one["trend_score"] = 0.0
        one.loc[one["slope"] > 0, "trend_score"] += 10.0
        one.loc[one["slope"] > 0.01, "trend_score"] += 10.0

        one["momentum_score"] = 0.0
        one.loc[one["macd"] > 0, "momentum_score"] += 10.0
        one.loc[one["rsi"] > 50, "momentum_score"] += 5.0

        one["velocity_score"] = 0.0
        one.loc[one["slope_atr_scaled"] > 0, "velocity_score"] += 10.0
        one.loc[one["slope_atr_scaled"] > 0.01, "velocity_score"] += 10.0

        one["penalty_score"] = 0.0
        one.loc[one["rsi"] >= 85, "penalty_score"] += 5.0
        one.loc[one["close"] <= 0, "penalty_score"] += 20.0

        one["score_buy"] = (
            one["base_score"]
            + one["trend_score"]
            + one["momentum_score"]
            + one["velocity_score"]
        )
        one["score_sell"] = one["penalty_score"]
        one["score_slope"] = one["trend_score"]

        if "score_mtf" not in one.columns:
            one["score_mtf"] = 0.0

        one["score_total"] = one["score_buy"] - one["score_sell"]
        one["score"] = one["score_total"]
        one["final_score"] = one["score_total"] + pd.to_numeric(one["score_mtf"], errors="coerce").fillna(0.0)
        one["display_score"] = one["final_score"]

        parts.append(one)

    enriched = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    enriched = sort_symbol_dt(enriched)

    logger.warning(
        "[INCREMENTAL SUMMARY] indicator rescue applied interval=%s rows=%s symbols=%s",
        interval,
        len(enriched),
        enriched["symbol"].nunique() if "symbol" in enriched.columns else 0,
    )
    log_indicator_profile(f"{interval}m-after-force-enrich", enriched)
    return enriched


def rescue_if_needed(df: pd.DataFrame, interval: int, stage: str) -> pd.DataFrame:
    out = ensure_df(df)
    if needs_indicator_rescue(out):
        logger.warning(
            "[INCREMENTAL SUMMARY] indicator rescue start interval=%s stage=%s rows=%s",
            interval,
            stage,
            len(out),
        )
        out = force_enrich_indicators(out, interval=interval)
    return out