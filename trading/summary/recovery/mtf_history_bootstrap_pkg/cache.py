# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/cache.py
# Version: PRODUCTION-STABLE-REV1.0-CACHE
# ------------------------------------------------------------
# 【概要】
#   global_data cache への latest per symbol 投入
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .datetime_guard import drop_future_datetime_rows
from .dataframe_utils import latest_per_symbol

logger = logging.getLogger(__name__)

try:
    from global_state import global_data
except Exception:
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None


def set_global_cache(df: pd.DataFrame, *, interval: int) -> None:
    """
    表示用 global_data には latest だけ入れる。
    indicator/scoring はこの関数より前に full history へ実行済みであること。
    """
    if global_data is None or df is None or df.empty:
        return

    df = drop_future_datetime_rows(df, interval=int(interval), label="before_global_cache")
    if df.empty:
        logger.warning("[MTF HISTORY BOOTSTRAP] global cache skipped no valid datetime interval=%s", interval)
        return

    latest = latest_per_symbol(df)

    if latest.empty:
        return

    latest = drop_future_datetime_rows(latest, interval=int(interval), label="global_cache_latest")
    if latest.empty:
        logger.warning("[MTF HISTORY BOOTSTRAP] global cache latest skipped no valid datetime interval=%s", interval)
        return

    try:
        if hasattr(global_data, "set_merged_summary"):
            try:
                global_data.set_merged_summary(int(interval), latest, source="push")
            except TypeError:
                global_data.set_merged_summary(int(interval), latest)
        elif hasattr(global_data, "set_push_merged_summary"):
            global_data.set_push_merged_summary(int(interval), latest)
        else:
            setattr(global_data, f"merged_summary_{int(interval)}", latest)

        logger.info(
            "[MTF HISTORY BOOTSTRAP] global cache set interval=%s rows=%s symbols=%s latest_dt=%s "
            "score_nonzero=%s rsi_nonnull=%s macd_nonnull=%s slope_nonnull=%s mtf_nonnull=%s datetime_nonnull=%s",
            interval,
            len(latest),
            latest["symbol"].nunique() if "symbol" in latest.columns else 0,
            latest["datetime"].max() if "datetime" in latest.columns and not latest.empty else None,
            int(pd.to_numeric(latest["score"], errors="coerce").fillna(0).ne(0).sum()) if "score" in latest.columns else 0,
            int(pd.to_numeric(latest["rsi"], errors="coerce").notna().sum()) if "rsi" in latest.columns else 0,
            int(pd.to_numeric(latest["macd"], errors="coerce").notna().sum()) if "macd" in latest.columns else 0,
            int(pd.to_numeric(latest["slope"], errors="coerce").notna().sum()) if "slope" in latest.columns else 0,
            int(pd.to_numeric(latest["mtf"], errors="coerce").notna().sum()) if "mtf" in latest.columns else 0,
            int(latest["datetime"].notna().sum()) if "datetime" in latest.columns else 0,
        )

    except Exception:
        logger.exception("[MTF HISTORY BOOTSTRAP] global cache set failed interval=%s", interval)


__all__ = [
    "set_global_cache",
]