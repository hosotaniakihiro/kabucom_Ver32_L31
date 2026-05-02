# ============================================================
# File   : core/startup/summary_runtime_pkg/db_seed_diagnostics.py
# Version: REV1.0-SUMMARY-RUNTIME-DB-SEED-DIAGNOSTICS
# ------------------------------------------------------------
# 【概要】
#   起動時 summary DB seed 用の診断ログ
#
# 【主な機能】
#   ✔ symbol ごとの履歴本数 profile
#   ✔ indicator / scoring profile
#   ✔ shortage warning
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .dataframe_utils import symbols_count
from .db_seed_policy import (
    get_required_hint,
    latest_dt,
    nonnull_count,
    nonzero_count,
)

logger = logging.getLogger(__name__)


def safe_symbols_count(df: pd.DataFrame) -> int:
    try:
        return int(symbols_count(df))
    except Exception:
        try:
            if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
                return int(
                    df["symbol"]
                    .astype(str)
                    .str.strip()
                    .replace("", pd.NA)
                    .dropna()
                    .nunique()
                )
        except Exception:
            pass
    return 0


def log_history_quality(df: pd.DataFrame, *, tf: int, bars: int, label: str) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            logger.warning(
                "[summary_runtime] %s tf=%s history empty",
                label,
                tf,
            )
            return

        counts = df.groupby("symbol").size()
        if counts.empty:
            logger.warning(
                "[summary_runtime] %s tf=%s history counts empty",
                label,
                tf,
            )
            return

        required = get_required_hint(tf, bars)
        shortage = int((counts < required).sum())

        logger.info(
            "[summary_runtime] %s tf=%s history_quality rows=%d symbols=%d "
            "bars=%d min=%d median=%d max=%d shortage_lt_%d=%d latest_dt=%s",
            label,
            tf,
            len(df),
            int(counts.shape[0]),
            int(bars),
            int(counts.min()),
            int(counts.median()),
            int(counts.max()),
            int(required),
            shortage,
            latest_dt(df),
        )

        if shortage:
            logger.warning(
                "[summary_runtime] %s tf=%s history shortage symbols=%d/%d "
                "required_hint=%d median=%d => boot indicators may be unstable",
                label,
                tf,
                shortage,
                int(counts.shape[0]),
                int(required),
                int(counts.median()),
            )

    except Exception:
        logger.debug(
            "[summary_runtime] history quality log failed tf=%s label=%s",
            tf,
            label,
            exc_info=True,
        )


def log_indicator_profile(df: pd.DataFrame, *, tf: int, label: str) -> None:
    try:
        logger.info(
            "[summary_runtime] %s tf=%s indicator_profile rows=%d symbols=%d "
            "score=%d final_score=%d display_score=%d score_buy=%d score_sell=%d "
            "slope=%d slope_atr_scaled=%d score_slope=%d "
            "mtf=%d score_mtf=%d mtf_score=%d "
            "rsi_nonnull=%d macd_nonnull=%d signal_nonnull=%d close_nonnull=%d latest_dt=%s",
            label,
            tf,
            len(df) if isinstance(df, pd.DataFrame) else 0,
            safe_symbols_count(df),
            nonzero_count(df, "score"),
            nonzero_count(df, "final_score"),
            nonzero_count(df, "display_score"),
            nonzero_count(df, "score_buy"),
            nonzero_count(df, "score_sell"),
            nonzero_count(df, "slope"),
            nonzero_count(df, "slope_atr_scaled"),
            nonzero_count(df, "score_slope"),
            nonzero_count(df, "mtf"),
            nonzero_count(df, "score_mtf"),
            nonzero_count(df, "mtf_score"),
            nonnull_count(df, "rsi"),
            nonnull_count(df, "macd"),
            nonnull_count(df, "signal"),
            nonnull_count(df, "close"),
            latest_dt(df),
        )
    except Exception:
        logger.debug(
            "[summary_runtime] indicator profile log failed tf=%s label=%s",
            tf,
            label,
            exc_info=True,
        )


__all__ = [
    "safe_symbols_count",
    "log_history_quality",
    "log_indicator_profile",
]