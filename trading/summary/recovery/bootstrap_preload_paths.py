# ============================================================
# File   : trading/summary/recovery/bootstrap_preload_paths.py
# Ver    : PRODUCTION-STABLE-REV1.0-BOOTSTRAP-PRELOAD-PATHS
# ------------------------------------------------------------
# ✔ skip rebuild path
# ✔ delta empty preload path
# ✔ cache fallback helpers
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.summary.recovery.helpers import normalize_datetime_columns
from trading.summary.recovery.persistence import finalize_for_upsert, update_global_cache
from .preload import (
    load_recent_history_for_cache,
    resample_htf_from_1m,
    restore_recent_persisted_summary_to_cache,
    trim_htf_cache_bars,
)
from .bootstrap_logging import log_source_date_breakdown
from .bootstrap_transforms import apply_indicators_and_scoring

logger = logging.getLogger(__name__)


def run_skip_rebuild_restore_path(
    *,
    update_cache: bool,
    last_1m_dt,
    last_3m_dt,
    last_5m_dt,
    dates,
    anchor_day,
    max_allowed_dt,
    warmup_bars_3m: int,
    warmup_bars_5m: int,
    SNAPSHOT_PRELOAD_MIN_BARS_1M: int,
    SNAPSHOT_PRELOAD_MIN_BARS_3M: int,
    SNAPSHOT_PRELOAD_MIN_BARS_5M: int,
    SNAPSHOT_PRELOAD_BUFFER_BARS_1M: int,
    SNAPSHOT_PRELOAD_BUFFER_BARS_3M: int,
    SNAPSHOT_PRELOAD_BUFFER_BARS_5M: int,
):
    restored_1m = pd.DataFrame()
    restored_3m = pd.DataFrame()
    restored_5m = pd.DataFrame()

    if update_cache:
        restored_1m = restore_recent_persisted_summary_to_cache(
            1,
            last_1m_dt,
            min_bars=SNAPSHOT_PRELOAD_MIN_BARS_1M + SNAPSHOT_PRELOAD_BUFFER_BARS_1M,
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )
        restored_3m = restore_recent_persisted_summary_to_cache(
            3,
            last_3m_dt,
            min_bars=max(warmup_bars_3m, SNAPSHOT_PRELOAD_MIN_BARS_3M) + SNAPSHOT_PRELOAD_BUFFER_BARS_3M,
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )
        restored_5m = restore_recent_persisted_summary_to_cache(
            5,
            last_5m_dt,
            min_bars=max(warmup_bars_5m, SNAPSHOT_PRELOAD_MIN_BARS_5M) + SNAPSHOT_PRELOAD_BUFFER_BARS_5M,
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )

    return restored_1m, restored_3m, restored_5m


def run_delta_empty_preload_path(
    *,
    last_1m_dt,
    dates,
    anchor_day,
    max_allowed_dt,
    warmup_bars_3m: int,
    warmup_bars_5m: int,
    update_cache: bool,
    SNAPSHOT_PRELOAD_MIN_BARS_1M: int,
    SNAPSHOT_PRELOAD_MIN_BARS_3M: int,
    SNAPSHOT_PRELOAD_MIN_BARS_5M: int,
    SNAPSHOT_PRELOAD_BUFFER_BARS_1M: int,
    SNAPSHOT_PRELOAD_BUFFER_BARS_3M: int,
    SNAPSHOT_PRELOAD_BUFFER_BARS_5M: int,
):
    preload_1m_raw = load_recent_history_for_cache(
        1,
        last_1m_dt,
        min_bars=SNAPSHOT_PRELOAD_MIN_BARS_1M + SNAPSHOT_PRELOAD_BUFFER_BARS_1M,
        target_dates_ctx=dates,
        anchor_day=anchor_day,
        max_allowed_dt=max_allowed_dt,
    )
    preload_1m_raw = normalize_datetime_columns(preload_1m_raw, interval=1)

    log_source_date_breakdown(
        preload_1m_raw,
        label="preload_1m_raw",
        target_dates_ctx=dates,
        anchor_day=anchor_day,
        required_bars_per_symbol=SNAPSHOT_PRELOAD_MIN_BARS_1M + SNAPSHOT_PRELOAD_BUFFER_BARS_1M,
    )

    preload_1m_raw = apply_indicators_and_scoring(
        preload_1m_raw,
        interval=1,
        label="preload_1m",
    )
    preload_1m = finalize_for_upsert(preload_1m_raw, 1)

    preload_3m_raw = resample_htf_from_1m(preload_1m_raw, 3)
    preload_5m_raw = resample_htf_from_1m(preload_1m_raw, 5)

    preload_3m_raw = normalize_datetime_columns(preload_3m_raw, interval=3)
    preload_5m_raw = normalize_datetime_columns(preload_5m_raw, interval=5)

    preload_3m_raw = apply_indicators_and_scoring(
        preload_3m_raw,
        interval=3,
        label="preload_3m",
    )
    preload_5m_raw = apply_indicators_and_scoring(
        preload_5m_raw,
        interval=5,
        label="preload_5m",
    )

    preload_3m = finalize_for_upsert(preload_3m_raw, 3)
    preload_5m = finalize_for_upsert(preload_5m_raw, 5)

    preload_3m = trim_htf_cache_bars(
        preload_3m,
        3,
        keep_bars=max(warmup_bars_3m, SNAPSHOT_PRELOAD_MIN_BARS_3M) + SNAPSHOT_PRELOAD_BUFFER_BARS_3M,
    )
    preload_5m = trim_htf_cache_bars(
        preload_5m,
        5,
        keep_bars=max(warmup_bars_5m, SNAPSHOT_PRELOAD_MIN_BARS_5M) + SNAPSHOT_PRELOAD_BUFFER_BARS_5M,
    )

    preload_3m = normalize_datetime_columns(preload_3m, interval=3)
    preload_5m = normalize_datetime_columns(preload_5m, interval=5)

    log_source_date_breakdown(
        preload_3m_raw,
        label="preload_3m_from_1m",
        target_dates_ctx=dates,
        anchor_day=anchor_day,
    )
    log_source_date_breakdown(
        preload_5m_raw,
        label="preload_5m_from_1m",
        target_dates_ctx=dates,
        anchor_day=anchor_day,
    )

    if update_cache:
        if not preload_1m_raw.empty:
            update_global_cache(preload_1m_raw, 1)
        if not preload_3m_raw.empty:
            update_global_cache(preload_3m_raw, 3)
        if not preload_5m_raw.empty:
            update_global_cache(preload_5m_raw, 5)

    return preload_1m, preload_3m, preload_5m, preload_1m_raw, preload_3m_raw, preload_5m_raw