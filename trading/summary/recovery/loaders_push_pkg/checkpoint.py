# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/checkpoint.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-CHECKPOINT
# ------------------------------------------------------------
# 【概要】
#   checkpoint 以降の PUSH tick filter
#
# 【主な機能】
#   ✔ normalize_push_df
#   ✔ future tick guard
#   ✔ market session filter
#   ✔ last_dt より後の tick 抽出
#   ✔ received_at による rescue
#
# 【重要】
#   - last_dt / tick_time / received_at は比較前に tz-naive 化
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from trading.summary.recovery.helpers import safe_get_series
from trading.summary.recovery.loaders_common import (
    now_naive,
    sanitize_checkpoint_dt,
)

from .filters import (
    filter_future_ticks,
    filter_market_session_ticks,
)
from .normalizer import normalize_push_df
from .timezone import (
    to_tz_naive_datetime_series,
    to_tz_naive_timestamp,
)

logger = logging.getLogger(__name__)


def filter_push_after(
    df_push: pd.DataFrame,
    last_dt: Optional[pd.Timestamp],
    *,
    now: Optional[dt.datetime] = None,
    drop_future_ticks: bool = True,
    market_hours_only: bool = False,
) -> pd.DataFrame:
    df_push = normalize_push_df(df_push)
    if df_push.empty:
        return df_push

    now_safe = (
        to_tz_naive_timestamp(now, label="filter_push_after.now")
        if now is not None
        else to_tz_naive_timestamp(now_naive(), label="filter_push_after.now_naive")
    )

    if drop_future_ticks:
        df_push = filter_future_ticks(
            df_push,
            datetime_col="tick_time",
            now_dt=now_safe,
            tolerance_minutes=2,
            label="filter_push_after.pre",
        )

    if market_hours_only:
        df_push = filter_market_session_ticks(
            df_push,
            datetime_col="tick_time",
            label="filter_push_after.pre",
        )

    if df_push.empty:
        logger.info(
            "[summary.recovery.loaders_push.checkpoint] filter_push_after empty after pre-filters"
        )
        return df_push

    if last_dt is None or pd.isna(last_dt):
        logger.info(
            "[summary.recovery.loaders_push.checkpoint] filter_push_after full-pass rows=%d (last_dt is None)",
            len(df_push),
        )
        return df_push.copy().reset_index(drop=True)

    last_dt = sanitize_checkpoint_dt(
        last_dt,
        label="filter_push_after.last_dt",
        interval=None,
    )

    last_dt_safe = to_tz_naive_timestamp(
        last_dt,
        label="filter_push_after.last_dt_sanitized",
    )

    if last_dt_safe is None or pd.isna(last_dt_safe):
        logger.info(
            "[summary.recovery.loaders_push.checkpoint] filter_push_after full-pass rows=%d (last_dt sanitized to None)",
            len(df_push),
        )
        return df_push.copy().reset_index(drop=True)

    try:
        tick_s = to_tz_naive_datetime_series(
            safe_get_series(df_push, "tick_time"),
            label="filter_push_after.tick_time",
        )

        df_push = df_push.loc[tick_s.notna()].copy()
        tick_s = tick_s.loc[df_push.index]

        try:
            df_push["tick_time"] = tick_s
        except Exception:
            pass

        mask = tick_s > last_dt_safe

        if not mask.any() and "received_at" in df_push.columns:
            recv_s = to_tz_naive_datetime_series(
                safe_get_series(df_push, "received_at"),
                label="filter_push_after.received_at",
            )
            recv_s = recv_s.loc[df_push.index]
            recv_mask = recv_s > last_dt_safe

            if recv_mask.any():
                logger.warning(
                    "[summary.recovery.loaders_push.checkpoint] filter_push_after rescued by received_at last_dt=%s tick_max=%s recv_max=%s rescued_rows=%d",
                    last_dt_safe,
                    tick_s.max(),
                    recv_s.max(),
                    int(recv_mask.sum()),
                )
                mask = recv_mask

        out = df_push.loc[mask].copy().reset_index(drop=True)

        logger.info(
            "[summary.recovery.loaders_push.checkpoint] filter_push_after last_dt=%s input_rows=%d output_rows=%d tick_min=%s tick_max=%s",
            last_dt_safe,
            len(df_push),
            len(out),
            tick_s.min(),
            tick_s.max(),
        )

        return out

    except Exception:
        logger.exception("[summary.recovery.loaders_push.checkpoint] filter_push_after failed")
        return pd.DataFrame()


__all__ = [
    "filter_push_after",
]