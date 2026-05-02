# ============================================================
# File   : trading/yahoo/pipeline/complement/compute.py
# Version: PRODUCTION-STABLE-REV4.1-YAHOO-COMPLEMENT-COMPUTE
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完サマリーの計算・整形
#
# 【主な機能】
#   - time_range / start_time / end_time 生成
#   - indicator計算
#   - scoring_pipeline実行
#   - score / final_score / display_score 保証
#   - summary schema整形
#   - source付与
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.scoring.core.scoring_pipeline import run_scoring_pipeline

from .constants import (
    PREFERRED_SUMMARY_COLUMNS,
    yahoo_source_for_interval,
)
from .normalize import (
    safe_df,
    normalize_datetime_df,
    numeric_series,
    coalesce_series,
    backfill_symbolname,
)

logger = logging.getLogger(__name__)


# ============================================================
# time helpers
# ============================================================

def build_time_range_from_datetime(dt_series: pd.Series, interval: int) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        end = base.dt.floor("min")
        start = end - pd.to_timedelta(max(int(interval) - 1, 0), unit="min")
        return start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")
    except Exception:
        logger.exception("[YAHOO COMPUTE] build time_range failed interval=%s", interval)
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def build_start_time(dt_series: pd.Series, interval: int) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        start = base.dt.floor("min") - pd.to_timedelta(max(int(interval) - 1, 0), unit="min")
        return start.dt.strftime("%H:%M:%S")
    except Exception:
        logger.exception("[YAHOO COMPUTE] build start_time failed interval=%s", interval)
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def build_end_time(dt_series: pd.Series) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        return base.dt.strftime("%H:%M:%S")
    except Exception:
        logger.exception("[YAHOO COMPUTE] build end_time failed")
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


# ============================================================
# prepare
# ============================================================

def prepare_summary_frame(
    df: pd.DataFrame,
    interval: int,
    *,
    source: str | None = None,
) -> pd.DataFrame:
    work = safe_df(df)
    if work.empty:
        return work

    work = normalize_datetime_df(work)
    if work.empty:
        return work

    interval = int(interval)
    source = source or yahoo_source_for_interval(interval)

    for col in ["open", "high", "low", "close", "volume"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    work["open_price"] = pd.to_numeric(work["open"], errors="coerce")
    work["high_price"] = pd.to_numeric(work["high"], errors="coerce")
    work["low_price"] = pd.to_numeric(work["low"], errors="coerce")
    work["close_price"] = pd.to_numeric(work["close"], errors="coerce")

    work["open"] = work["open_price"]
    work["high"] = work["high_price"]
    work["low"] = work["low_price"]
    work["close"] = work["close_price"]

    work["date"] = work["datetime"].dt.strftime("%Y-%m-%d")
    work["time_range"] = build_time_range_from_datetime(work["datetime"], interval)
    work["time"] = work["datetime"].dt.strftime("%H:%M:%S")
    work["start_time"] = build_start_time(work["datetime"], interval)
    work["end_time"] = build_end_time(work["datetime"])

    work["interval"] = interval
    work["source"] = source

    work["price"] = work["close"]
    work["current_price"] = work["close"]
    work["trading_volume"] = work["volume"]

    work["last_update"] = pd.Timestamp.now()

    if "signal" not in work.columns:
        work["signal"] = ""

    work = backfill_symbolname(work)

    return work


# ============================================================
# indicators / scoring
# ============================================================

def apply_indicators(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        try:
            ret = add_all_indicators(out, interval=int(interval))
        except TypeError:
            try:
                ret = add_all_indicators(out, interval=f"{int(interval)}min")
            except TypeError:
                ret = add_all_indicators(out)

        if isinstance(ret, pd.DataFrame):
            out = ret

        logger.info(
            "[YAHOO COMPUTE] indicators done interval=%s rows=%s",
            interval,
            len(out),
        )

    except Exception:
        logger.exception("[YAHOO COMPUTE] indicator calculation failed interval=%s", interval)

    return safe_df(out)


def apply_scoring(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        try:
            ret = run_scoring_pipeline(out, interval=f"{int(interval)}min")
        except TypeError:
            try:
                ret = run_scoring_pipeline(out, f"{int(interval)}min")
            except TypeError:
                ret = run_scoring_pipeline(out)

        if isinstance(ret, pd.DataFrame):
            out = ret

        logger.info(
            "[YAHOO COMPUTE] scoring done interval=%s rows=%s",
            interval,
            len(out),
        )

    except Exception:
        logger.exception("[YAHOO COMPUTE] scoring failed interval=%s", interval)

    return safe_df(out)


# ============================================================
# scores / schema
# ============================================================

def ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = safe_df(df)
        if out.empty:
            return pd.DataFrame()

        score_buy = numeric_series(out, "score_buy").fillna(0.0)
        score_sell = numeric_series(out, "score_sell").fillna(0.0)
        score_total = numeric_series(out, "score_total").fillna(0.0)

        score = numeric_series(out, "score")
        final_score = numeric_series(out, "final_score")
        display_score = numeric_series(out, "display_score")

        slope = numeric_series(out, "slope")
        slope_alt = numeric_series(out, "slope_atr_scaled")
        mtf = numeric_series(out, "mtf")
        score_mtf = numeric_series(out, "score_mtf")

        if (slope.fillna(0) == 0).all() and not (slope_alt.fillna(0) == 0).all():
            slope = coalesce_series(slope.replace(0, pd.NA), slope_alt)

        if (mtf.fillna(0) == 0).all() and not (score_mtf.fillna(0) == 0).all():
            mtf = coalesce_series(mtf.replace(0, pd.NA), score_mtf)

        composed = score.copy()
        if (composed.fillna(0) == 0).all():
            if not (final_score.fillna(0) == 0).all():
                composed = final_score.copy()
            elif not (display_score.fillna(0) == 0).all():
                composed = display_score.copy()
            elif not (score_total.fillna(0) == 0).all():
                composed = score_total.copy()
            else:
                buy_abs = score_buy.abs()
                sell_abs = score_sell.abs()
                composed = score_buy.where(buy_abs >= sell_abs, score_sell)

        if (final_score.fillna(0) == 0).all():
            final_score = composed.copy()

        if (display_score.fillna(0) == 0).all():
            display_score = final_score.copy()

        out["score_buy"] = score_buy.fillna(0.0)
        out["score_sell"] = score_sell.fillna(0.0)
        out["score_total"] = score_total.fillna(0.0)
        out["score"] = composed.fillna(0.0)
        out["final_score"] = final_score.fillna(0.0)
        out["display_score"] = display_score.fillna(0.0)

        out["slope"] = slope.fillna(0.0)
        out["mtf"] = mtf.fillna(0.0)
        out["slope_atr_scaled"] = numeric_series(out, "slope_atr_scaled").fillna(out["slope"])
        out["score_mtf"] = numeric_series(out, "score_mtf").fillna(out["mtf"])

        if "buy_score" not in out.columns:
            out["buy_score"] = out["score_buy"]
        if "sell_score" not in out.columns:
            out["sell_score"] = out["score_sell"]

        return out

    except Exception:
        logger.exception("[YAHOO COMPUTE] ensure score columns failed")
        return pd.DataFrame()


def ensure_summary_schema_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        out = safe_df(df)
        if out.empty:
            return pd.DataFrame()

        out = normalize_datetime_df(out)
        if out.empty:
            return pd.DataFrame()

        interval = int(interval)

        for src, dst in [
            ("open", "open_price"),
            ("high", "high_price"),
            ("low", "low_price"),
            ("close", "close_price"),
        ]:
            if src in out.columns and dst not in out.columns:
                out[dst] = pd.to_numeric(out[src], errors="coerce")

        for src, dst in [
            ("open_price", "open"),
            ("high_price", "high"),
            ("low_price", "low"),
            ("close_price", "close"),
        ]:
            if src in out.columns and dst not in out.columns:
                out[dst] = pd.to_numeric(out[src], errors="coerce")

        if "date" not in out.columns:
            out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")

        if "time_range" not in out.columns:
            out["time_range"] = build_time_range_from_datetime(out["datetime"], interval)

        if "time" not in out.columns:
            out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

        if "start_time" not in out.columns:
            out["start_time"] = build_start_time(out["datetime"], interval)

        if "end_time" not in out.columns:
            out["end_time"] = build_end_time(out["datetime"])

        out["interval"] = interval
        out["source"] = yahoo_source_for_interval(interval)

        out = backfill_symbolname(out)

        if "signal" not in out.columns:
            out["signal"] = ""

        if "volume" not in out.columns:
            out["volume"] = 0.0

        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)

        if "last_update" not in out.columns:
            out["last_update"] = pd.Timestamp.now()

        for col in [
            "rsi", "macd", "hist", "ma5", "ma25", "ma75",
            "ema12", "ema26", "atr",
            "bb_mid", "bb_upper", "bb_lower", "bb_width",
            "score_buy", "score_sell", "score_total",
            "score", "final_score", "display_score",
            "slope", "slope_atr_scaled", "mtf", "score_mtf",
        ]:
            if col not in out.columns:
                out[col] = 0.0

        return out

    except Exception:
        logger.exception("[YAHOO COMPUTE] ensure summary schema columns failed")
        return pd.DataFrame()


def finalize_before_save(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        out = safe_df(df)
        if out.empty:
            return pd.DataFrame()

        out = normalize_datetime_df(out)
        if out.empty:
            return pd.DataFrame()

        if "symbol" not in out.columns or "datetime" not in out.columns:
            logger.warning("[YAHOO COMPUTE] finalize missing key columns")
            return pd.DataFrame()

        out = (
            out.dropna(subset=["symbol", "datetime"])
               .sort_values(["symbol", "datetime"], kind="stable")
               .drop_duplicates(subset=["symbol", "datetime"], keep="last")
               .reset_index(drop=True)
        )

        if out.empty:
            return pd.DataFrame()

        out = backfill_symbolname(out)
        out = ensure_score_columns(out)
        if out.empty:
            return pd.DataFrame()

        out = ensure_summary_schema_columns(out, interval=interval)
        if out.empty:
            return pd.DataFrame()

        out = (
            out.sort_values(["symbol", "datetime"], kind="stable")
               .drop_duplicates(subset=["symbol", "datetime"], keep="last")
               .reset_index(drop=True)
        )

        existing = [c for c in PREFERRED_SUMMARY_COLUMNS if c in out.columns]
        others = [c for c in out.columns if c not in existing]
        out = out[existing + others].copy()

        return out

    except Exception:
        logger.exception("[YAHOO COMPUTE] finalize before save failed")
        return pd.DataFrame()


def warn_if_suspicious_zero_scores(df: pd.DataFrame) -> None:
    try:
        if df is None or df.empty:
            return

        score_zero = (numeric_series(df, "score").fillna(0) == 0).all()
        final_zero = (numeric_series(df, "final_score").fillna(0) == 0).all()
        display_zero = (numeric_series(df, "display_score").fillna(0) == 0).all()

        signal_cols = [
            c for c in [
                "_score_base",
                "_score_trend",
                "_score_mom",
                "_score_velocity",
                "score_buy",
                "score_sell",
                "score_total",
                "score_mtf",
                "slope",
                "slope_atr_scaled",
                "mtf",
                "rsi",
                "macd",
                "hist",
            ] if c in df.columns
        ]

        signal_nonzero = False
        for c in signal_cols:
            s = numeric_series(df, c).fillna(0)
            if (s != 0).any():
                signal_nonzero = True
                break

        if score_zero and final_zero and display_zero and signal_nonzero:
            logger.warning(
                "[YAHOO COMPUTE] suspicious zero-score frame rows=%d symbols=%d signal_cols=%s",
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else 0,
                signal_cols,
            )
    except Exception:
        logger.debug("[YAHOO COMPUTE] zero-score anomaly check failed", exc_info=True)


def compute_summary_frame(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    1 interval分のサマリー計算をまとめて実行する。
    """
    out = prepare_summary_frame(df, interval=interval)
    if out.empty:
        return out

    out = apply_indicators(out, interval=interval)
    if out.empty:
        return out

    out = apply_scoring(out, interval=interval)
    if out.empty:
        return out

    out = finalize_before_save(out, interval=interval)
    if out.empty:
        return out

    warn_if_suspicious_zero_scores(out)

    logger.info(
        "[YAHOO COMPUTE] computed interval=%s rows=%s symbols=%s latest=%s score_nonzero=%s",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        int((numeric_series(out, "score").fillna(0) != 0).sum()) if not out.empty else 0,
    )

    return out


__all__ = [
    "build_time_range_from_datetime",
    "build_start_time",
    "build_end_time",
    "prepare_summary_frame",
    "apply_indicators",
    "apply_scoring",
    "ensure_score_columns",
    "ensure_summary_schema_columns",
    "finalize_before_save",
    "warn_if_suspicious_zero_scores",
    "compute_summary_frame",
]