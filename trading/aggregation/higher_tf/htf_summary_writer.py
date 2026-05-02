"""
============================================================
htf_summary_writer.py
Higher Timeframe Summary Writer
------------------------------------------------------------
✔ summary DB保存
✔ merged_summary同期
✔ DataFrame安全化
✔ SQLite / DuckDB互換
✔ NaN / inf防御
✔ dtype崩壊防止
✔ 本番運用安定版
============================================================
"""

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame安全化
    """

    try:

        df = df.copy()

        # NaN / inf 防御
        df.replace([np.inf, -np.inf], np.nan, inplace=True)

        for col in df.columns:

            if df[col].dtype.kind in {"f", "i"}:

                df[col] = df[col].fillna(0.0)

            else:

                df[col] = df[col].where(
                    pd.notna(df[col]),
                    None
                )

        return df

    except Exception:

        logger.exception("[HTF sanitize failed]")

        return df


# ============================================================
# DB SAVE
# ============================================================

def save_summary(df: pd.DataFrame, interval: int):

    """
    summary DB保存
    """

    try:

        if df is None or df.empty:
            return

        df = _sanitize_dataframe(df)

        bulk_upsert_summary(
            df,
            interval=interval
        )

    except Exception:

        logger.exception("[HTF summary save failed]")


# ============================================================
# merged_summary SYNC
# ============================================================

def sync_merged_summary(df: pd.DataFrame, interval: int):

    """
    global_data merged_summary 更新
    """

    try:

        if df is None or df.empty:
            return

        df = _sanitize_dataframe(df)

        current = global_data.get_merged_summary(interval)

        if current is None or current.empty:

            df_updated = df.copy()

        else:

            current = current.copy()

            current["datetime"] = pd.to_datetime(
                current["datetime"],
                errors="coerce"
            )

            df_updated = pd.concat(
                [current, df],
                ignore_index=True
            )

        df_updated = (
            df_updated
            .sort_values(["symbol", "datetime"])
            .drop_duplicates(
                ["symbol", "datetime"],
                keep="last"
            )
            .reset_index(drop=True)
        )

        global_data.set_merged_summary(
            interval,
            df_updated
        )

    except Exception:

        logger.exception("[HTF merged_summary sync failed]")