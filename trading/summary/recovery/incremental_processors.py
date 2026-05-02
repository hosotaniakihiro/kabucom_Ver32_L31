# ============================================================
# File   : trading/summary/recovery/incremental_processors.py
# Ver    : PRODUCTION-STABLE-REV1.2-SUMMARY-RECOVERY-INCREMENTAL-ARGFIX
# ------------------------------------------------------------
# 【概要】
#   summary recovery 用の差分処理群
#
# 【主な修正】
#   - update_global_cache の引数順修正
#   - raw / upsert 用 DF を分離
#   - guard/filter 後の raw を cache 更新へ渡す
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from trading.summary.recovery.helpers import merge_summary_frames_with_priority
from trading.summary.recovery.persistence import (
    finalize_for_upsert,
    update_global_cache,
    upsert_summary_df,
)
from trading.summary.recovery.rebuilders import (
    RECENT_RECALC_BARS_1M,
    rebuild_1min_from_push,
    rebuild_higher_tf_from_1m,
    trim_recent_bars,
)
from trading.summary.recovery.guards import guard_future_rows
from trading.summary.recovery.market_hours import filter_market_hours_rows

logger = logging.getLogger(__name__)


def process_incremental_1m(
    df_push: pd.DataFrame,
    *,
    existing_1m: Optional[pd.DataFrame] = None,
    persist: bool = True,
    update_cache: bool = True,
) -> pd.DataFrame:
    try:
        delta_1m = rebuild_1min_from_push(df_push)
        if delta_1m.empty:
            logger.info("[summary_recovery.incremental] process_incremental_1m skipped: delta_1m empty")
            return pd.DataFrame()

        full_1m_raw = merge_summary_frames_with_priority(existing_1m, delta_1m, interval=1)
        full_1m_raw = trim_recent_bars(full_1m_raw, bars=RECENT_RECALC_BARS_1M)
        full_1m_raw = guard_future_rows(full_1m_raw, 1, label="process_incremental_1m")
        full_1m_raw = filter_market_hours_rows(full_1m_raw, 1, label="process_incremental_1m")

        full_1m_upsert = finalize_for_upsert(full_1m_raw, 1)

        if persist and not full_1m_upsert.empty:
            upsert_summary_df(full_1m_upsert, 1)

        if update_cache and not full_1m_raw.empty:
            update_global_cache(full_1m_raw, 1)

        logger.info(
            "[summary_recovery.incremental] process_incremental_1m done raw_rows=%d upsert_rows=%d persist=%s update_cache=%s",
            len(full_1m_raw),
            len(full_1m_upsert),
            persist,
            update_cache,
        )
        return full_1m_upsert

    except Exception:
        logger.exception("[summary_recovery.incremental] process_incremental_1m failed")
        return pd.DataFrame()


def process_incremental_higher_tf(
    df_1m: pd.DataFrame,
    interval: int,
    *,
    persist: bool = True,
    update_cache: bool = True,
) -> pd.DataFrame:
    try:
        if int(interval) not in (3, 5):
            raise ValueError(f"unsupported interval={interval}")

        out_raw = rebuild_higher_tf_from_1m(df_1m, int(interval))
        if out_raw.empty:
            logger.info(
                "[summary_recovery.incremental] process_incremental_higher_tf skipped: interval=%s empty",
                interval,
            )
            return pd.DataFrame()

        out_raw = guard_future_rows(out_raw, int(interval), label=f"process_incremental_{interval}m")
        out_raw = filter_market_hours_rows(out_raw, int(interval), label=f"process_incremental_{interval}m")

        out_upsert = finalize_for_upsert(out_raw, int(interval))

        if persist and not out_upsert.empty:
            upsert_summary_df(out_upsert, int(interval))

        if update_cache and not out_raw.empty:
            update_global_cache(out_raw, int(interval))

        logger.info(
            "[summary_recovery.incremental] process_incremental_higher_tf done interval=%s raw_rows=%d upsert_rows=%d persist=%s update_cache=%s",
            interval,
            len(out_raw),
            len(out_upsert),
            persist,
            update_cache,
        )
        return out_upsert

    except Exception:
        logger.exception(
            "[summary_recovery.incremental] process_incremental_higher_tf failed interval=%s",
            interval,
        )
        return pd.DataFrame()


__all__ = [
    "process_incremental_1m",
    "process_incremental_higher_tf",
]