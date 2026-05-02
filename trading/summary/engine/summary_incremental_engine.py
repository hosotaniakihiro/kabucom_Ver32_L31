# ============================================================
# File   : trading/summary/engine/incremental/pipeline.py
# Version: Ver1.1-INCREMENTAL-PIPELINE
#          -INDICATOR-FORCE-ENRICH
#          -LATEST-RESCUE
#          -PROFILE-ENHANCED
# ------------------------------------------------------------
# 修正:
#   - safe_indicator / safe_mtf / safe_scoring 後に空指標を救済
#   - OHLCしか無い場合でも RSI/MACD/ATR/slope/score を強制補完
#   - latest 抽出後にも指標列状態を検査
#   - summary_latest_df を安定的に DB 保存・表示へ渡す
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd

from trading.summary.calculator.summary_pipeline import calculate_summary
from trading.summary.engine.guards.enhance_guard import enhance_guard
from trading.summary.engine.guards.pre_db_guard import pre_db_guard
from trading.summary.engine.internal.scoring_guard import finalize_scoring
from trading.summary.engine.processors.indicator import safe_indicator
from trading.summary.engine.processors.mtf import safe_mtf
from trading.summary.engine.processors.resample import safe_resample
from trading.summary.engine.processors.scoring import safe_scoring

from .common import (
    empty_result,
    interval_label,
    log_df_state,
    profile_numeric_state,
    safe_upsert,
)
from .history import merge_with_history, store_merged_summary_safe
from .metrics import ensure_slope, rebuild_scaled_slope
from .timeframe import (
    dedupe_prefer_completed_rows,
    drop_future_rows,
    extract_latest_timeframe,
    normalize_intraday_bar_times,
)

logger = logging.getLogger(__name__)


# ============================================================
# local helpers
# ============================================================

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


def _ensure_df(df: pd.DataFrame | None) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df.copy()
    return pd.DataFrame()


def _to_numeric(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
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


def _sort_symbol_dt(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df)
    if out.empty:
        return out
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str)
    if "symbol" in out.columns and "datetime" in out.columns:
        out = out.sort_values(["symbol", "datetime"], kind="stable")
    return out.reset_index(drop=True)


def _safe_non_null(df: pd.DataFrame, col: str) -> int:
    try:
        if col in df.columns:
            return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        pass
    return 0


def _safe_non_zero(df: pd.DataFrame, col: str) -> int:
    try:
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            return int((s.fillna(0) != 0).sum())
    except Exception:
        pass
    return 0


def _log_indicator_profile(tag: str, df: pd.DataFrame) -> None:
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
                    _safe_non_null(df, c),
                    _safe_non_zero(df, c),
                )
    except Exception:
        logger.exception("[INCREMENTAL SUMMARY] _log_indicator_profile failed tag=%s", tag)


def _has_ohlc(df: pd.DataFrame) -> bool:
    return all(c in df.columns for c in ("open", "high", "low", "close"))


def _needs_indicator_rescue(df: pd.DataFrame) -> bool:
    """
    OHLC はあるが指標列が無い・ほぼ空のとき救済する。
    """
    if df is None or df.empty:
        return False
    if not _has_ohlc(df):
        return False
    if "symbol" not in df.columns or "datetime" not in df.columns:
        return False

    missing = [c for c in ("rsi", "macd", "signal", "atr", "slope") if c not in df.columns]
    if missing:
        return True

    total_non_null = sum(_safe_non_null(df, c) for c in ("rsi", "macd", "signal", "atr", "slope"))
    return total_non_null == 0


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _calc_macd(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    ema12 = series.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = series.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    return macd, signal


def _calc_atr(one: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = one["close"].shift(1)
    tr1 = one["high"] - one["low"]
    tr2 = (one["high"] - prev_close).abs()
    tr3 = (one["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _calc_slope(series: pd.Series, period: int = 5) -> pd.Series:
    return (series - series.shift(period)) / series.shift(period).replace(0, np.nan)


def _force_enrich_indicators(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    safe_indicator が空振りしても、最低限の指標を付与して後段を止めない。
    """
    out = _ensure_df(df)
    if out.empty:
        return out
    if not _has_ohlc(out):
        return out

    out = _ensure_datetime(out)
    out = _to_numeric(out, ["open", "high", "low", "close", "volume", "turnover", "vwap"])
    out = out.dropna(subset=["datetime"]).copy()

    if "symbol" not in out.columns or out.empty:
        return out

    out["symbol"] = out["symbol"].astype(str)
    out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

    parts = []
    for symbol, one in out.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime").reset_index(drop=True)

        close = pd.to_numeric(one["close"], errors="coerce")
        one["rsi"] = _calc_rsi(close, period=14)
        one["macd"], one["signal"] = _calc_macd(close)
        one["atr"] = _calc_atr(one, period=14)
        one["slope"] = _calc_slope(close, period=5)
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
    enriched = _sort_symbol_dt(enriched)

    logger.warning(
        "[INCREMENTAL SUMMARY] indicator rescue applied interval=%s rows=%s symbols=%s",
        interval,
        len(enriched),
        enriched["symbol"].nunique() if "symbol" in enriched.columns else 0,
    )
    _log_indicator_profile(f"{interval}m-after-force-enrich", enriched)
    return enriched


def _rescue_if_needed(df: pd.DataFrame, interval: int, stage: str) -> pd.DataFrame:
    out = _ensure_df(df)
    if _needs_indicator_rescue(out):
        logger.warning(
            "[INCREMENTAL SUMMARY] indicator rescue start interval=%s stage=%s rows=%s",
            interval,
            stage,
            len(out),
        )
        out = _force_enrich_indicators(out, interval=interval)
    return out


# ============================================================
# builders
# ============================================================

def build_1m_from_push(df_push: pd.DataFrame) -> pd.DataFrame:
    if df_push is None or df_push.empty:
        return pd.DataFrame()

    symbols = df_push["symbol"].astype(str).unique().tolist() if "symbol" in df_push.columns else []

    try:
        df_1m = calculate_summary(
            df_push=df_push,
            symbols=symbols,
            start_time=None,
            end_time=None,
        )
    except TypeError:
        try:
            df_1m = calculate_summary(df_push)
        except Exception:
            logger.exception("[INCREMENTAL SUMMARY] calculate_summary fallback failed")
            df_1m = pd.DataFrame()
    except Exception:
        logger.exception("[INCREMENTAL SUMMARY] calculate_summary failed")
        df_1m = pd.DataFrame()

    df_1m = enhance_guard(df_1m)

    if df_1m.empty:
        df_1m = df_push.copy()
        if "close" in df_1m.columns:
            df_1m["close_price"] = df_1m["close"]
            df_1m["open_price"] = df_1m.get("open", df_1m["close"])
            df_1m["high_price"] = df_1m.get("high", df_1m["close"])
            df_1m["low_price"] = df_1m.get("low", df_1m["close"])

    df_1m = _ensure_datetime(df_1m)
    log_df_state("1m-after-calculate", df_1m)
    return df_1m


def build_target_interval_df(df_push: pd.DataFrame, interval: int) -> pd.DataFrame:
    interval = int(interval)

    df_1m = build_1m_from_push(df_push)
    if df_1m.empty:
        return pd.DataFrame()

    if interval == 1:
        return df_1m
    if interval == 3:
        df = safe_resample(df_1m, 3)
        df = _ensure_datetime(df)
        log_df_state("3m-after-resample", df)
        return df
    if interval == 5:
        df = safe_resample(df_1m, 5)
        df = _ensure_datetime(df)
        log_df_state("5m-after-resample", df)
        return df

    logger.warning("[INCREMENTAL SUMMARY] unsupported interval=%s", interval)
    return pd.DataFrame()


# ============================================================
# main
# ============================================================

def process_single_interval(df_push: pd.DataFrame, interval: int) -> dict:
    interval = int(interval)
    label = f"{interval}m"

    logger.info("[INCREMENTAL SUMMARY] single-interval start interval=%s", interval)

    df = build_target_interval_df(df_push, interval)
    if df.empty:
        logger.warning("[INCREMENTAL SUMMARY] target df empty interval=%s", interval)
        return empty_result(interval)

    df = merge_with_history(df, interval)
    df = _ensure_datetime(df)
    df = _sort_symbol_dt(df)
    log_df_state(f"{label}-after-history", df)

    # --------------------------------------------------------
    # indicator / slope / mtf / scoring
    # --------------------------------------------------------
    df = safe_indicator(df)
    df = ensure_slope(df)
    df = _rescue_if_needed(df, interval=interval, stage="after-safe-indicator")
    log_df_state(f"{label}-after-indicator-slope", df)
    profile_numeric_state(f"{label}-after-indicator-slope", df)
    _log_indicator_profile(f"{label}-after-indicator-slope", df)

    df = safe_mtf(df)
    df = _rescue_if_needed(df, interval=interval, stage="after-safe-mtf")
    profile_numeric_state(f"{label}-after-mtf", df)
    _log_indicator_profile(f"{label}-after-mtf", df)

    df = safe_scoring(df, interval_label(interval))
    df = _rescue_if_needed(df, interval=interval, stage="after-safe-scoring")
    profile_numeric_state(f"{label}-after-scoring", df)
    _log_indicator_profile(f"{label}-after-scoring", df)

    df = finalize_scoring(enhance_guard(df))
    df = rebuild_scaled_slope(df)
    df = _rescue_if_needed(df, interval=interval, stage="after-finalize")
    profile_numeric_state(f"{label}-after-finalize", df)
    _log_indicator_profile(f"{label}-after-finalize", df)

    if "score" in df.columns and "score_sell" not in df.columns:
        df["score_sell"] = -pd.to_numeric(df["score"], errors="coerce").fillna(0)

    df = normalize_intraday_bar_times(df, interval)
    df = _ensure_datetime(df)
    df = _sort_symbol_dt(df)
    log_df_state(f"{label}-before-latest", df)

    # --------------------------------------------------------
    # latest timeframe
    # --------------------------------------------------------
    df_latest = extract_latest_timeframe(df, interval=interval)
    df_latest = normalize_intraday_bar_times(df_latest, interval)
    df_latest = drop_future_rows(df_latest, tolerance_seconds=60)
    df_latest = dedupe_prefer_completed_rows(df_latest)
    df_latest = _ensure_datetime(df_latest)
    df_latest = _sort_symbol_dt(df_latest)
    df_latest = _rescue_if_needed(df_latest, interval=interval, stage="after-extract-latest")

    log_df_state(f"{label}-latest", df_latest)
    profile_numeric_state(f"{label}-latest", df_latest)
    _log_indicator_profile(f"{label}-latest", df_latest)

    # --------------------------------------------------------
    # pre DB guard
    # --------------------------------------------------------
    df_latest = pre_db_guard(df_latest, interval)
    df_latest = normalize_intraday_bar_times(df_latest, interval)
    df_latest = drop_future_rows(df_latest, tolerance_seconds=60)
    df_latest = dedupe_prefer_completed_rows(df_latest)
    df_latest = _ensure_datetime(df_latest)
    df_latest = _sort_symbol_dt(df_latest)
    df_latest = _rescue_if_needed(df_latest, interval=interval, stage="after-pre-db-guard")

    log_df_state(f"{label}-after-pre-db-guard", df_latest)
    profile_numeric_state(f"{label}-after-pre-db-guard", df_latest)
    _log_indicator_profile(f"{label}-after-pre-db-guard", df_latest)

    # --------------------------------------------------------
    # save & cache
    # --------------------------------------------------------
    safe_upsert(df_latest, interval)
    store_merged_summary_safe(interval, df)

    logger.info("[INCREMENTAL SUMMARY] single-interval finished interval=%s", interval)

    return {
        "interval": interval,
        "summary_df": df if isinstance(df, pd.DataFrame) else pd.DataFrame(),
        "summary_latest_df": df_latest if isinstance(df_latest, pd.DataFrame) else pd.DataFrame(),
    }