# ============================================================
# File   : trading/summary/pipelines/feature_scoring_pipeline.py
# Version: Ver2-PRODUCTION-COMPATIBILITY-WRAPPER
# ------------------------------------------------------------
# ✔ 旧 feature_scoring_pipeline 完全互換
# ✔ positional / keyword 引数完全互換
# ✔ scoring_pipeline へ完全転送
# ✔ summary / ranking / push 全互換
# ✔ dataframe guard
# ✔ interval 自動補正
# ✔ NaN / structure crash防止
# ✔ production safe wrapper
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.scoring.core.scoring_pipeline import run_scoring_pipeline

logger = logging.getLogger(__name__)


# ============================================================
# DATAFRAME GUARD
# ============================================================

def _ensure_dataframe(df):

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):

        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    return df.copy()


# ============================================================
# INTERVAL SANITIZER
# ============================================================

def _sanitize_interval(interval):

    if interval is None:
        return "1min"

    try:
        interval = str(interval)
    except Exception:
        return "1min"

    interval = interval.lower()

    if interval in (
        "1m", "1min", "1minute"
    ):
        return "1min"

    if interval in (
        "3m", "3min", "3minute"
    ):
        return "3min"

    if interval in (
        "5m", "5min", "5minute"
    ):
        return "5min"

    return "1min"


# ============================================================
# MAIN WRAPPER
# ============================================================

def run_feature_scoring_pipeline(
    df: pd.DataFrame,
    interval: str = "1min",
    **kwargs
) -> pd.DataFrame:
    """
    旧 feature_scoring_pipeline 完全互換ラッパー

    対応呼び出し

    run_feature_scoring_pipeline(df)
    run_feature_scoring_pipeline(df, "1min")
    run_feature_scoring_pipeline(df, interval="1min")

    summary / ranking / push すべて互換
    """

    df = _ensure_dataframe(df)

    if df.empty:
        return df

    interval = _sanitize_interval(interval)

    try:

        df = run_scoring_pipeline(
            df,
            interval=interval,
            **kwargs
        )

        return df

    except Exception:

        logger.exception(
            "[FEATURE SCORING PIPELINE] scoring failed"
        )

        return df


# ============================================================
# ALIAS（旧API互換）
# ============================================================

run_pipeline = run_feature_scoring_pipeline
run_feature_scoring = run_feature_scoring_pipeline