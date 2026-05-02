# ============================================================
# File   : trading/ranking/summary/technicals_adapter.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-TECHNICALS-ADAPTER
# ------------------------------------------------------------
# 【概要】
#   technical_from_ranking 呼び出し + fallback technical
# ============================================================

from __future__ import annotations

import datetime as dt
import inspect
import logging
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


try:
    from trading.ranking.summary.technical_from_ranking import (
        build_ranking_summary_technical as _external_build_ranking_summary_technical,
        get_latest_ranking_summary_rows as _external_get_latest_ranking_summary_rows,
    )
except Exception:
    _external_build_ranking_summary_technical = None
    _external_get_latest_ranking_summary_rows = None
    logger.warning(
        "[RANKING SUMMARY RUNNER] technical_from_ranking import failed -> fallback technical will be used",
        exc_info=True,
    )


def fallback_add_indicators(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    x = x.sort_values(["symbol", "datetime"], kind="mergesort")

    try:
        x["close"] = pd.to_numeric(x["close"], errors="coerce")
    except Exception:
        pass

    frames: list[pd.DataFrame] = []

    for _, g in x.groupby("symbol", sort=False):
        gg = g.copy()

        try:
            close = pd.to_numeric(gg["close"], errors="coerce")
            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.rolling(14, min_periods=3).mean()
            avg_loss = loss.rolling(14, min_periods=3).mean()
            rs = avg_gain / avg_loss.replace(0, pd.NA)
            gg["rsi"] = 100 - (100 / (1 + rs))
        except Exception:
            gg["rsi"] = pd.NA

        try:
            ema12 = pd.to_numeric(gg["close"], errors="coerce").ewm(span=12, adjust=False).mean()
            ema26 = pd.to_numeric(gg["close"], errors="coerce").ewm(span=26, adjust=False).mean()
            macd = ema12 - ema26
            signal = macd.ewm(span=9, adjust=False).mean()
            gg["macd"] = macd
            gg["signal"] = signal
            gg["macd_hist"] = macd - signal
            gg["hist"] = gg["macd_hist"]
        except Exception:
            gg["macd"] = pd.NA
            gg["signal"] = pd.NA
            gg["macd_hist"] = pd.NA
            gg["hist"] = pd.NA

        try:
            gg["slope"] = pd.to_numeric(gg["close"], errors="coerce").pct_change(3) * 100.0
            gg["score_slope"] = gg["slope"]
        except Exception:
            gg["slope"] = pd.NA
            gg["score_slope"] = pd.NA

        frames.append(gg)

    if not frames:
        return x

    out = pd.concat(frames, ignore_index=True)
    out["interval"] = int(interval)

    return out


def call_external_technical(
    base_df: pd.DataFrame,
    *,
    interval: int,
    trade_date: Optional[str | int | dt.date | dt.datetime],
    lookback_minutes: int,
    symbols: Optional[Iterable[str]],
    ranking_db_path: Optional[str],
    yahoo_db_path: Optional[str],
    use_yahoo_fill: bool,
) -> pd.DataFrame:
    if not callable(_external_build_ranking_summary_technical):
        logger.warning(
            "[RANKING SUMMARY RUNNER] external technical unavailable -> fallback"
        )
        return fallback_add_indicators(base_df, interval=interval)

    fn = _external_build_ranking_summary_technical

    kwargs = {
        "interval": interval,
        "trade_date": trade_date,
        "lookback_minutes": lookback_minutes,
        "symbols": symbols,
        "ranking_db_path": ranking_db_path,
        "yahoo_db_path": yahoo_db_path,
        "use_yahoo_fill": use_yahoo_fill,
    }

    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )

        if accepts_var_kw:
            call_kwargs = kwargs
        else:
            call_kwargs = {k: v for k, v in kwargs.items() if k in params}

        try:
            return fn(base_df, **call_kwargs)
        except TypeError:
            logger.exception(
                "[RANKING SUMMARY RUNNER] technical build TypeError interval=%s -> retry df only",
                interval,
            )
            return fn(base_df)

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] external technical failed interval=%s -> fallback",
            interval,
        )
        return fallback_add_indicators(base_df, interval=interval)


def get_latest_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if callable(_external_get_latest_ranking_summary_rows):
        try:
            latest = _external_get_latest_ranking_summary_rows(df)
            if isinstance(latest, pd.DataFrame) and not latest.empty:
                return latest.reset_index(drop=True)
        except Exception:
            logger.exception(
                "[RANKING SUMMARY RUNNER] external latest rows failed -> fallback latest per symbol"
            )

    try:
        x = df.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x[x["datetime"].notna()].copy()

        if x.empty:
            return pd.DataFrame()

        latest_dt = x["datetime"].max()
        latest = x[x["datetime"] == latest_dt].copy()

        if latest["symbol"].nunique() < max(5, int(x["symbol"].nunique() * 0.1)):
            idx = x.sort_values("datetime").groupby("symbol", sort=False).tail(1).index
            latest = x.loc[idx].copy()

        return latest.reset_index(drop=True)

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] fallback latest rows failed"
        )
        return df.tail(100).copy().reset_index(drop=True)