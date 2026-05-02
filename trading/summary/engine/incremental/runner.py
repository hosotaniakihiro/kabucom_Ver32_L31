# ============================================================
# File   : trading/summary/engine/incremental/runner.py
# Version: Ver1.1-INCREMENTAL-RUNNER-INDICATOR-INTERVAL-PASS
# ------------------------------------------------------------
# ✔ single interval orchestration
# ✔ build -> history -> indicator/mtf/scoring -> latest -> save
# ✔ pipeline.py から実行責務を分離
# ✔ safe_indicator に interval を明示伝搬
# ✔ 3分足 / 5分足でも indicator_calculator の maturity guard を正しく適用
# ✔ profile / indicator logs 維持
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.summary.engine.guards.enhance_guard import enhance_guard
from trading.summary.engine.guards.pre_db_guard import pre_db_guard
from trading.summary.engine.internal.scoring_guard import finalize_scoring
from trading.summary.engine.processors.indicator import safe_indicator
from trading.summary.engine.processors.mtf import safe_mtf
from trading.summary.engine.processors.scoring import safe_scoring

from .builders import build_target_interval_df
from .common import (
    empty_result,
    interval_label,
    log_df_state,
    profile_numeric_state,
    safe_upsert,
)
from .enrich import (
    ensure_datetime,
    rescue_if_needed,
    sort_symbol_dt,
    log_indicator_profile,
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


def process_single_interval(df_push: pd.DataFrame, interval: int) -> dict:
    interval = int(interval)
    label = f"{interval}m"
    iv_label = interval_label(interval)

    logger.info(
        "[INCREMENTAL SUMMARY] single-interval start interval=%s interval_label=%s",
        interval,
        iv_label,
    )

    df = build_target_interval_df(df_push, interval)
    if df.empty:
        logger.warning("[INCREMENTAL SUMMARY] target df empty interval=%s", interval)
        return empty_result(interval)

    df = merge_with_history(df, interval)
    df = ensure_datetime(df)
    df = sort_symbol_dt(df)
    log_df_state(f"{label}-after-history", df)

    # --------------------------------------------------------
    # indicator / slope / mtf / scoring
    # --------------------------------------------------------
    df = safe_indicator(df, interval=iv_label)
    df = ensure_slope(df)
    df = rescue_if_needed(df, interval=interval, stage="after-safe-indicator")
    log_df_state(f"{label}-after-indicator-slope", df)
    profile_numeric_state(f"{label}-after-indicator-slope", df)
    log_indicator_profile(f"{label}-after-indicator-slope", df)

    df = safe_mtf(df)
    df = rescue_if_needed(df, interval=interval, stage="after-safe-mtf")
    profile_numeric_state(f"{label}-after-mtf", df)
    log_indicator_profile(f"{label}-after-mtf", df)

    df = safe_scoring(df, iv_label)
    df = rescue_if_needed(df, interval=interval, stage="after-safe-scoring")
    profile_numeric_state(f"{label}-after-scoring", df)
    log_indicator_profile(f"{label}-after-scoring", df)

    df = finalize_scoring(enhance_guard(df))
    df = rebuild_scaled_slope(df)
    df = rescue_if_needed(df, interval=interval, stage="after-finalize")
    profile_numeric_state(f"{label}-after-finalize", df)
    log_indicator_profile(f"{label}-after-finalize", df)

    if "score" in df.columns and "score_sell" not in df.columns:
        df["score_sell"] = -pd.to_numeric(df["score"], errors="coerce").fillna(0)

    df = normalize_intraday_bar_times(df, interval)
    df = ensure_datetime(df)
    df = sort_symbol_dt(df)
    log_df_state(f"{label}-before-latest", df)

    # --------------------------------------------------------
    # latest timeframe
    # --------------------------------------------------------
    df_latest = extract_latest_timeframe(df, interval=interval)
    df_latest = normalize_intraday_bar_times(df_latest, interval)
    df_latest = drop_future_rows(df_latest, tolerance_seconds=60)
    df_latest = dedupe_prefer_completed_rows(df_latest)
    df_latest = ensure_datetime(df_latest)
    df_latest = sort_symbol_dt(df_latest)
    df_latest = rescue_if_needed(df_latest, interval=interval, stage="after-extract-latest")

    log_df_state(f"{label}-latest", df_latest)
    profile_numeric_state(f"{label}-latest", df_latest)
    log_indicator_profile(f"{label}-latest", df_latest)

    # --------------------------------------------------------
    # pre DB guard
    # --------------------------------------------------------
    df_latest = pre_db_guard(df_latest, interval)
    df_latest = normalize_intraday_bar_times(df_latest, interval)
    df_latest = drop_future_rows(df_latest, tolerance_seconds=60)
    df_latest = dedupe_prefer_completed_rows(df_latest)
    df_latest = ensure_datetime(df_latest)
    df_latest = sort_symbol_dt(df_latest)
    df_latest = rescue_if_needed(df_latest, interval=interval, stage="after-pre-db-guard")

    log_df_state(f"{label}-after-pre-db-guard", df_latest)
    profile_numeric_state(f"{label}-after-pre-db-guard", df_latest)
    log_indicator_profile(f"{label}-after-pre-db-guard", df_latest)

    # --------------------------------------------------------
    # save & cache
    # --------------------------------------------------------
    safe_upsert(df_latest, interval)
    store_merged_summary_safe(interval, df)

    logger.info(
        "[INCREMENTAL SUMMARY] single-interval finished interval=%s interval_label=%s",
        interval,
        iv_label,
    )

    return {
        "interval": interval,
        "summary_df": df if isinstance(df, pd.DataFrame) else pd.DataFrame(),
        "summary_latest_df": df_latest if isinstance(df_latest, pd.DataFrame) else pd.DataFrame(),
    }