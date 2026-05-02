# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/filters.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-FILTERS
# ------------------------------------------------------------
# 【概要】
#   PUSH tick filter
#
# 【主な機能】
#   ✔ future tick guard
#   ✔ market session filter
#   ✔ tz-naive datetime 比較
#
# 【重要】
#   - 比較前に datetime は timezone なしへ統一する
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from trading.summary.recovery.helpers import safe_get_series
from trading.summary.recovery.loaders_common import now_naive

from .constants import (
    MARKET_AM_START,
    MARKET_AM_END,
    MARKET_PM_START,
    MARKET_PM_END,
)
from .normalizer import drop_duplicate_columns
from .timezone import (
    to_tz_naive_datetime_series,
    to_tz_naive_timestamp,
)

logger = logging.getLogger(__name__)


def filter_future_ticks(
    df: pd.DataFrame,
    *,
    datetime_col: str = "tick_time",
    now_dt: Optional[pd.Timestamp] = None,
    tolerance_minutes: int = 2,
    label: str = "",
) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return df

        x = drop_duplicate_columns(df.copy(), label=f"{label}.future_guard")

        if datetime_col not in x.columns:
            return x

        s = safe_get_series(x, datetime_col)
        s = to_tz_naive_datetime_series(s, label=f"{label}.{datetime_col}.future.pre")

        if now_dt is None or pd.isna(now_dt):
            now_base = now_naive()
        else:
            now_base = now_dt

        now_ts = to_tz_naive_timestamp(now_base, label=f"{label}.now_dt")
        if now_ts is None:
            now_ts = to_tz_naive_timestamp(now_naive(), label=f"{label}.now_naive_fallback")

        max_dt = now_ts + pd.Timedelta(minutes=tolerance_minutes)

        before = len(x)

        x = x.loc[s.notna()].copy()
        s = s.loc[x.index]

        try:
            x[datetime_col] = s
        except Exception:
            pass

        mask = s <= max_dt
        out = x.loc[mask].copy().reset_index(drop=True)

        dropped = int(before - len(out))
        if dropped > 0:
            logger.warning(
                "[summary.recovery.loaders_push.filters] future ticks removed label=%s dropped=%d now_dt=%s max_dt=%s tolerance_min=%s",
                label,
                dropped,
                now_ts,
                max_dt,
                tolerance_minutes,
            )

        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_push.filters] filter_future_ticks failed label=%s",
            label,
        )
        return df


def filter_market_session_ticks(
    df: pd.DataFrame,
    *,
    datetime_col: str = "tick_time",
    label: str = "",
) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return df

        x = drop_duplicate_columns(df.copy(), label=f"{label}.session_guard")

        if datetime_col not in x.columns:
            return x

        s = safe_get_series(x, datetime_col)
        s = to_tz_naive_datetime_series(s, label=f"{label}.{datetime_col}.session.pre")

        x = x.loc[s.notna()].copy()
        s = s.loc[x.index]

        try:
            x[datetime_col] = s
        except Exception:
            pass

        hhmm = s.dt.hour * 100 + s.dt.minute

        am_mask = (hhmm >= MARKET_AM_START) & (hhmm <= MARKET_AM_END)
        pm_mask = (hhmm >= MARKET_PM_START) & (hhmm <= MARKET_PM_END)
        mask = am_mask | pm_mask

        before = len(x)
        out = x.loc[mask].copy().reset_index(drop=True)
        dropped = int(before - len(out))

        if dropped > 0:
            logger.warning(
                "[summary.recovery.loaders_push.filters] out-of-session ticks removed label=%s dropped=%d",
                label,
                dropped,
            )

        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_push.filters] filter_market_session_ticks failed label=%s",
            label,
        )
        return df


# ------------------------------------------------------------
# Backward-compatible aliases
# ------------------------------------------------------------
_filter_future_ticks = filter_future_ticks
_filter_market_session_ticks = filter_market_session_ticks


__all__ = [
    "filter_future_ticks",
    "filter_market_session_ticks",
    "_filter_future_ticks",
    "_filter_market_session_ticks",
]