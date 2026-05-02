"""
============================================================
htf_indicator_pipeline.py
Higher Timeframe Indicator + Scoring Pipeline
------------------------------------------------------------
✔ indicator計算
✔ scoring計算
✔ DataFrame安全化
✔ NaN / inf 防御
✔ dtype崩壊防止
✔ HTF専用パイプライン
✔ 本番安定版
✔ FIX: scoring_main の循環 import 回避（遅延 import）
============================================================
"""

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.summary.incremental_indicators import add_all_indicators

logger = logging.getLogger(__name__)


# ============================================================
# Lazy import helper
# ============================================================

def _get_scoring_main():
    """
    循環 import 回避のため、scoring_main は関数内で遅延 import する。
    """
    from trading.scoring.core.scoring_core import scoring_main
    return scoring_main


# ============================================================
# Utility
# ============================================================

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame安全化
    """
    try:
        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        if df.empty:
            return df

        df = df.copy()

        # NaN / inf 防御
        df.replace([np.inf, -np.inf], np.nan, inplace=True)

        for col in df.columns:
            try:
                if hasattr(df[col], "dtype") and df[col].dtype.kind in {"f", "i"}:
                    df[col] = df[col].fillna(0.0)
                else:
                    df[col] = df[col].where(pd.notna(df[col]), None)
            except Exception:
                logger.debug("[HTF sanitize column failed] col=%s", col, exc_info=True)

        return df

    except Exception:
        logger.exception("[HTF sanitize failed]")
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


# ============================================================
# PIPELINE
# ============================================================

def run_pipeline(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    HTF indicator + scoring pipeline
    """
    try:
        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        if df.empty:
            return df

        df = df.copy()

        # --------------------------------------------------
        # indicator
        # --------------------------------------------------
        try:
            df = add_all_indicators(df, interval=interval)
        except TypeError:
            try:
                df = add_all_indicators(df)
            except Exception:
                logger.exception("[HTF indicator failed]")
                return pd.DataFrame()
        except Exception:
            logger.exception("[HTF indicator failed]")
            return pd.DataFrame()

        if df is None or df.empty:
            return pd.DataFrame()

        # --------------------------------------------------
        # scoring
        # --------------------------------------------------
        try:
            scoring_main = _get_scoring_main()
            df = scoring_main(
                df,
                interval=interval
            )
        except Exception:
            logger.exception("[HTF scoring failed]")
            return pd.DataFrame()

        if df is None or df.empty:
            return pd.DataFrame()

        # --------------------------------------------------
        # DataFrame安全化
        # --------------------------------------------------
        df = _sanitize_dataframe(df)

        return df

    except Exception:
        logger.exception("[HTF pipeline fatal]")
        return pd.DataFrame()