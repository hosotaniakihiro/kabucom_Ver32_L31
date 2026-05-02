# ============================================================
# File   : trading/summary/recovery/preloaders.py
# Ver    : PRODUCTION-STABLE-REV1.2-SUMMARY-RECOVERY-PRELOADERS
# ------------------------------------------------------------
# 【概要】
#   summary recovery 用の起動時 preload / cache seed 支援
#
# 【主な機能】
#   - recent history preload
#   - source window clamp
#   - recent TF rows 制限
#   - cache seed build
#
# 【依存方針】
#   - guards / market_hours / helpers / loaders / persistence に依存
#   - incremental_processors / engine には依存しない
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.summary.recovery.helpers import (
    merge_summary_frames_with_priority,
    normalize_datetime_columns,
)
from trading.summary.recovery.loaders import (
    load_latest_summary_snapshot,
    load_summary_df_between,
)
from trading.summary.recovery.persistence import finalize_for_upsert
from trading.summary.recovery.guards import (
    guard_future_rows,
    normalize_dt_like,
)
from trading.summary.recovery.market_hours import filter_market_hours_rows

logger = logging.getLogger(__name__)

DELTA_SOURCE_SESSION_FLOOR_HOUR = 8


def history_window_by_interval(interval: int, bars: int) -> pd.Timedelta:
    interval = int(interval)
    bars = max(int(bars), 1)
    total_min = interval * bars
    return pd.Timedelta(minutes=total_min)


def load_recent_history_for_cache(
    interval: int,
    last_dt,
    *,
    min_bars: int,
    fallback_snapshot: bool = True,
) -> pd.DataFrame:
    try:
        base_dt = normalize_dt_like(last_dt)
        if base_dt is None:
            base_dt = normalize_dt_like(pd.Timestamp.now())

        if base_dt is None:
            logger.warning(
                "[summary_recovery.preloaders] recent history preload skipped: interval=%s base_dt unresolved",
                interval,
            )
            return pd.DataFrame()

        window = history_window_by_interval(interval, min_bars)
        start_dt = base_dt - window
        end_dt = base_dt + pd.Timedelta(minutes=max(int(interval), 1))

        hist = load_summary_df_between(int(interval), start_dt, end_dt)
        hist = normalize_datetime_columns(hist, interval=int(interval))
        hist = guard_future_rows(
            hist,
            int(interval),
            label=f"recent_history_preload_{interval}m",
        )
        hist = filter_market_hours_rows(
            hist,
            int(interval),
            label=f"recent_history_preload_{interval}m",
        )
        hist = finalize_for_upsert(hist, int(interval))

        if hist.empty and fallback_snapshot:
            snap = load_latest_summary_snapshot(int(interval))
            snap = normalize_datetime_columns(snap, interval=int(interval))
            snap = guard_future_rows(
                snap,
                int(interval),
                label=f"recent_history_snapshot_{interval}m",
            )
            snap = filter_market_hours_rows(
                snap,
                int(interval),
                label=f"recent_history_snapshot_{interval}m",
            )
            snap = finalize_for_upsert(snap, int(interval))
            logger.info(
                "[summary_recovery.preloaders] recent history fallback snapshot interval=%s rows=%d",
                interval,
                len(snap),
            )
            return snap

        logger.info(
            "[summary_recovery.preloaders] recent history loaded interval=%s rows=%d start=%s end=%s",
            interval,
            len(hist),
            start_dt,
            end_dt,
        )
        return hist

    except Exception:
        logger.exception(
            "[summary_recovery.preloaders] recent history preload failed interval=%s last_dt=%s",
            interval,
            last_dt,
        )
        return pd.DataFrame()


def clamp_start_dt_to_recent_session(start_dt, now_dt):
    try:
        start_dt = normalize_dt_like(start_dt)
        now_dt = normalize_dt_like(now_dt)

        if start_dt is None:
            return start_dt
        if now_dt is None:
            return start_dt

        session_floor = pd.Timestamp(now_dt.date()).replace(
            hour=DELTA_SOURCE_SESSION_FLOOR_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )

        clamped = max(start_dt, session_floor)
        if clamped != start_dt:
            logger.info(
                "[summary_recovery.preloaders] source window clamped start_dt=%s -> %s now_dt=%s",
                start_dt,
                clamped,
                now_dt,
            )
        return clamped

    except Exception:
        logger.exception(
            "[summary_recovery.preloaders] clamp start dt failed start_dt=%s now_dt=%s",
            start_dt,
            now_dt,
        )
        return start_dt


def limit_recent_tf_rows(
    df: pd.DataFrame,
    interval: int,
    *,
    keep_bars_per_symbol: int | None = None,
    keep_bars: int | None = None,
) -> pd.DataFrame:
    out = normalize_datetime_columns(df, interval=int(interval))
    if out.empty:
        return out

    if "symbol" not in out.columns or "datetime" not in out.columns:
        return out

    try:
        limit = keep_bars_per_symbol
        if limit is None:
            limit = keep_bars
        if limit is None:
            logger.info(
                "[summary_recovery.preloaders] limit recent tf rows skipped interval=%s reason=no_limit",
                interval,
            )
            return out

        keep_n = max(int(limit), 1)
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = (
            out.sort_values(["symbol", "datetime"])
            .groupby("symbol", group_keys=False)
            .tail(keep_n)
            .reset_index(drop=True)
        )
        logger.info(
            "[summary_recovery.preloaders] limited recent tf rows interval=%s keep_bars_per_symbol=%s rows=%d symbols=%d",
            interval,
            keep_n,
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns and not out.empty else 0,
        )
        return out
    except Exception:
        logger.exception(
            "[summary_recovery.preloaders] limit recent tf rows failed interval=%s keep_bars_per_symbol=%s keep_bars=%s",
            interval,
            keep_bars_per_symbol,
            keep_bars,
        )
        return out


def build_cache_seed_with_recent_history(
    interval: int,
    delta_df: pd.DataFrame,
    last_dt,
    *,
    min_bars: int,
) -> pd.DataFrame:
    try:
        hist = load_recent_history_for_cache(
            int(interval),
            last_dt,
            min_bars=max(int(min_bars), 1),
            fallback_snapshot=True,
        )
        hist = normalize_datetime_columns(hist, interval=int(interval))
        hist = guard_future_rows(
            hist,
            int(interval),
            label=f"cache_seed_hist_{interval}m",
        )
        hist = filter_market_hours_rows(
            hist,
            int(interval),
            label=f"cache_seed_hist_{interval}m",
        )
        hist = finalize_for_upsert(hist, int(interval))

        delta_df = normalize_datetime_columns(delta_df, interval=int(interval))
        delta_df = guard_future_rows(
            delta_df,
            int(interval),
            label=f"cache_seed_delta_{interval}m",
        )
        delta_df = filter_market_hours_rows(
            delta_df,
            int(interval),
            label=f"cache_seed_delta_{interval}m",
        )
        delta_df = finalize_for_upsert(delta_df, int(interval))

        merged = merge_summary_frames_with_priority(hist, delta_df, interval=int(interval))
        merged = normalize_datetime_columns(merged, interval=int(interval))
        merged = guard_future_rows(
            merged,
            int(interval),
            label=f"cache_seed_merged_{interval}m",
        )
        merged = filter_market_hours_rows(
            merged,
            int(interval),
            label=f"cache_seed_merged_{interval}m",
        )
        merged = finalize_for_upsert(merged, int(interval))

        if merged.empty:
            logger.info(
                "[summary_recovery.preloaders] cache seed empty interval=%s hist_rows=%d delta_rows=%d",
                interval,
                len(hist) if isinstance(hist, pd.DataFrame) else 0,
                len(delta_df) if isinstance(delta_df, pd.DataFrame) else 0,
            )
            return merged

        if "symbol" in merged.columns and "datetime" in merged.columns:
            merged["datetime"] = pd.to_datetime(merged["datetime"], errors="coerce")
            merged = (
                merged.dropna(subset=["symbol", "datetime"])
                .sort_values(["symbol", "datetime"])
                .groupby("symbol", group_keys=False)
                .tail(max(int(min_bars), 1))
                .reset_index(drop=True)
            )

        logger.info(
            "[summary_recovery.preloaders] cache seed built interval=%s rows=%d symbols=%d min_bars=%d",
            interval,
            len(merged),
            int(merged["symbol"].nunique()) if "symbol" in merged.columns and not merged.empty else 0,
            max(int(min_bars), 1),
        )
        return merged

    except Exception:
        logger.exception(
            "[summary_recovery.preloaders] build cache seed failed interval=%s min_bars=%s",
            interval,
            min_bars,
        )
        return pd.DataFrame()


__all__ = [
    "DELTA_SOURCE_SESSION_FLOOR_HOUR",
    "history_window_by_interval",
    "load_recent_history_for_cache",
    "clamp_start_dt_to_recent_session",
    "limit_recent_tf_rows",
    "build_cache_seed_with_recent_history",
]