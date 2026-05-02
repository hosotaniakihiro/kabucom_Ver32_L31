# ============================================================
# File   : trading/summary/pipeline/summary_pipeline.py
# Version: Ver1.0-PRODUCTION-SUMMARY-PIPELINE-INSTITUTIONAL
# ------------------------------------------------------------
# ✔ dataframe prepare pipeline
# ✔ feature + scoring pipeline
# ✔ signals pipeline
# ✔ duplicate column guard
# ✔ feature existence guard
# ✔ pandas alignment crash prevention
# ✔ NaN / inf safe
# ✔ realtime trading safe
# ✔ institutional production module
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

# ============================================================
# DATAFRAME PREPARE
# ============================================================

from trading.summary.engine.dataframe_prepare import (
    prepare_dataframe,
    normalize_columns,
)

# ============================================================
# FEATURE + SCORING
# ============================================================

from trading.summary.pipeline.feature_scoring_pipeline import (
    run_feature_scoring_pipeline
)

# ============================================================
# SIGNALS
# ============================================================

from trading.summary.pipeline.signals_pipeline import (
    run_signals_pipeline
)

# ============================================================
# DUPLICATE GUARD
# ============================================================

from trading.summary.utils.duplicate_guard import (
    guard_duplicates
)

# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# FEATURE EXISTENCE GUARD
# ============================================================

def ensure_feature_columns(df: pd.DataFrame) -> pd.DataFrame:

    required = [
        "ma5",
        "ma25",
        "ma75",
    ]

    try:

        for col in required:

            if col not in df.columns:

                df[col] = 0.0

    except Exception:

        logger.exception(
            "[SUMMARY PIPELINE] feature column ensure failed"
        )

    return df


# ============================================================
# MAIN SUMMARY PIPELINE
# ============================================================

def run_summary_pipeline(
    df: pd.DataFrame,
    interval: str = "1m"
) -> pd.DataFrame:

    if df is None or len(df) == 0:
        return pd.DataFrame()

    try:

        # ----------------------------------------------------
        # DATAFRAME PREPARE
        # ----------------------------------------------------

        df = prepare_dataframe(df)

        if df.empty:
            return df

        # ----------------------------------------------------
        # FEATURE + SCORING
        # ----------------------------------------------------

        df = run_feature_scoring_pipeline(df, interval)

        # duplicate column repair（重要）
        df = normalize_columns(df)

        # feature guard
        df = ensure_feature_columns(df)

        # ----------------------------------------------------
        # SIGNALS
        # ----------------------------------------------------

        df = run_signals_pipeline(df)

        # ----------------------------------------------------
        # FINAL GUARD
        # ----------------------------------------------------

        df = normalize_columns(df)

        df = guard_duplicates(df)

    except Exception:

        logger.exception(
            "[SUMMARY PIPELINE] pipeline failed"
        )

        return pd.DataFrame()

    return df