# ============================================================
# File   : trading/summary/recovery/bootstrap_orchestrator.py
# Ver    : PRODUCTION-STABLE-REV3.5-BOOTSTRAP-ORCHESTRATOR
#          -KEEP-SUMMARY-TAIL-MERGE-PUSH-REBUILD
#          -WARMUP-BOOST
#          -FULL-1M-FALLBACK
#          -TAIL-INDICATOR-SCORING
# ------------------------------------------------------------
# 【概要】
#   PUSH由来サマリーの起動時 incremental recovery orchestrator
#
# 【主な機能】
#   - startup delta-first orchestration
#   - split loaders 対応
#   - indicator/scoring 統合
#   - source breakdown / warmup log
#   - 同値バー保持
#   - preload / skip / normal path を分離
#   - 起動高速化:
#       - previous_business_day を必要時だけ使う
#       - runtime push を checkpoint 以降だけ読む
#       - delta symbols のみ higher tf 再構築
#       - latest snapshot restore を優先
#
# 【REV3.5 修正】
#   - 1m warmup を大幅増強
#   - summary tail / push supplement 後でも warmup 不足なら
#     recent 1m full fallback を発動
#   - symbolごとの tail 切り詰めを緩和
#   - bootstrap_1m / 3m / 5m を tail だけ indicator/scoring
#   - result に summary_1min / 3min / 5min を返す
#   - bootstrap_loaders.load_recent_1m_history_for_recalc の
#     実シグネチャに整合
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading.summary.recovery.helpers import (
    drop_rows_to_explicit_dates,
    extract_dates_from_datetime_like,
    is_today_business_day,
    merge_summary_frames_with_priority,
    normalize_datetime_columns,
    target_dates,
)
from trading.summary.recovery.persistence import (
    finalize_for_upsert,
    update_global_cache,
    upsert_summary_df,
)
from trading.summary.recovery.rebuilders import (
    calc_higher_tf_source_window,
    rebuild_1min_from_push,
    rebuild_higher_tf_from_1m,
)
from trading.summary.recovery.tail_processors import (
    apply_indicators_and_scoring_tail,
)

from .bootstrap_loaders import (
    filter_push_after,
    load_last_summary_datetime_compat,
    load_push_df_for_dates,
    load_recent_1m_history_for_recalc,
    load_runtime_push_delta_df,
    load_runtime_push_df,
    load_src_1m_tail_for_symbols,
    normalize_symbols,
)
from .bootstrap_logging import log_source_date_breakdown
from .bootstrap_preload_paths import (
    run_skip_rebuild_restore_path,
)
from .bootstrap_transforms import (
    keep_newer_or_equal_last_bar,
)
from .cache_seed import (
    build_cache_seed_with_recent_history,
    limit_recent_tf_rows,
)
from .checkpoints import (
    can_skip_rebuild_when_delta_empty,
    resolve_anchor_context,
)
from .preload import (
    clamp_start_dt_to_recent_session,
)

logger = logging.getLogger(__name__)


# ============================================================
# constants
# ============================================================

SNAPSHOT_PRELOAD_MIN_BARS_1M = 400
SNAPSHOT_PRELOAD_MIN_BARS_3M = 120
SNAPSHOT_PRELOAD_MIN_BARS_5M = 120

SNAPSHOT_PRELOAD_BUFFER_BARS_1M = 50
SNAPSHOT_PRELOAD_BUFFER_BARS_3M = 20
SNAPSHOT_PRELOAD_BUFFER_BARS_5M = 20

DELTA_KEEP_RECENT_BARS_3M = 3
DELTA_KEEP_RECENT_BARS_5M = 3

HTF_SOURCE_BUFFER_BARS_1M = 12
SMALL_DELTA_THRESHOLD_ROWS = 500
FULL_FALLBACK_BARS_1M = 500


# ============================================================
# small helpers
# ============================================================

def _bars_needed_for_higher_tf(interval: int, warmup_bars: int) -> int:
    interval = int(interval)
    warmup_bars = max(int(warmup_bars), 1)

    if interval == 3:
        return warmup_bars * 3 + HTF_SOURCE_BUFFER_BARS_1M
    if interval == 5:
        return warmup_bars * 5 + HTF_SOURCE_BUFFER_BARS_1M
    return warmup_bars + HTF_SOURCE_BUFFER_BARS_1M


def _should_include_previous_business_day(
    *,
    requested: bool,
    startup_delta_only: bool,
    last_1m_dt,
    last_3m_dt,
    last_5m_dt,
    market_open_today: bool,
) -> bool:
    if not requested:
        return False

    if startup_delta_only:
        if last_1m_dt is None or pd.isna(last_1m_dt):
            return True
        if last_3m_dt is None or pd.isna(last_3m_dt):
            return True
        if last_5m_dt is None or pd.isna(last_5m_dt):
            return True
        if not market_open_today:
            return True
        return False

    return bool(requested)


def _infer_delta_symbols(delta_push: pd.DataFrame) -> list[str]:
    try:
        if delta_push is None or delta_push.empty or "symbol" not in delta_push.columns:
            return []
        return normalize_symbols(delta_push["symbol"].astype(str).tolist())
    except Exception:
        logger.exception("[summary_recovery] infer delta symbols failed")
        return []


def _safe_update_cache(tf: int, df: pd.DataFrame) -> None:
    try:
        if df is None or df.empty:
            return
        update_global_cache(df, tf)
    except TypeError:
        try:
            update_global_cache(tf, df)
        except Exception:
            logger.debug("[summary_recovery] update_global_cache fallback failed tf=%s", tf, exc_info=True)
    except Exception:
        logger.debug("[summary_recovery] update_global_cache failed tf=%s", tf, exc_info=True)


def _safe_load_latest_snapshot(interval: int) -> pd.DataFrame:
    try:
        from .loaders_summary import load_latest_summary_snapshot
        return load_latest_summary_snapshot(interval)
    except Exception:
        logger.debug(
            "[summary_recovery] load_latest_summary_snapshot unavailable interval=%s",
            interval,
            exc_info=True,
        )
        return pd.DataFrame()


def _safe_load_recent_tail_default(
    interval: int,
    *,
    end_dt=None,
    target_dates=None,
    anchor_day=None,
    max_allowed_dt=None,
    symbols=None,
) -> pd.DataFrame:
    try:
        from .loaders_summary import load_recent_summary_tail_default
        return load_recent_summary_tail_default(
            interval=interval,
            end_dt=end_dt,
            target_dates=target_dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
            symbols=symbols,
        )
    except Exception:
        logger.debug(
            "[summary_recovery] load_recent_summary_tail_default unavailable interval=%s",
            interval,
            exc_info=True,
        )
        return pd.DataFrame()


def _restore_latest_snapshots_if_available(*, update_cache: bool) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    snap_1m = _safe_load_latest_snapshot(1)
    snap_3m = _safe_load_latest_snapshot(3)
    snap_5m = _safe_load_latest_snapshot(5)

    if update_cache:
        for tf, df in ((1, snap_1m), (3, snap_3m), (5, snap_5m)):
            if isinstance(df, pd.DataFrame) and not df.empty:
                _safe_update_cache(tf, df)

    return snap_1m, snap_3m, snap_5m


def _filter_df_from_start_dt(
    df: pd.DataFrame,
    start_dt,
    *,
    datetime_col: str = "datetime",
    label: str = "",
) -> pd.DataFrame:
    try:
        if df is None or df.empty or start_dt is None or pd.isna(start_dt):
            return df

        if datetime_col not in df.columns:
            return df

        x = df.copy()
        dt_s = pd.to_datetime(x[datetime_col], errors="coerce")
        start_ts = pd.to_datetime(start_dt, errors="coerce")

        out = x.loc[dt_s >= start_ts].copy().reset_index(drop=True)

        logger.info(
            "[summary_recovery] start_dt filter label=%s start_dt=%s before=%d after=%d",
            label,
            start_dt,
            len(df),
            len(out),
        )
        return out
    except Exception:
        logger.debug("[summary_recovery] start_dt filter failed label=%s", label, exc_info=True)
        return df


def _safe_symbol_count(df: pd.DataFrame) -> int:
    try:
        if df is None or df.empty or "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _bars_profile(df: pd.DataFrame) -> dict[str, Any]:
    try:
        if df is None or df.empty or "symbol" not in df.columns or "datetime" not in df.columns:
            return {"symbols": 0, "min": 0, "median": 0.0, "max": 0, "short": 0, "ok": 0}

        x = df.copy()
        x["symbol"] = x["symbol"].astype(str)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"])

        if x.empty:
            return {"symbols": 0, "min": 0, "median": 0.0, "max": 0, "short": 0, "ok": 0}

        counts = x.groupby("symbol")["datetime"].nunique()
        if counts.empty:
            return {"symbols": 0, "min": 0, "median": 0.0, "max": 0, "short": 0, "ok": 0}

        return {
            "symbols": int(counts.shape[0]),
            "min": int(counts.min()),
            "median": float(counts.median()),
            "max": int(counts.max()),
            "counts": counts,
        }
    except Exception:
        logger.debug("[summary_recovery] bars profile failed", exc_info=True)
        return {"symbols": 0, "min": 0, "median": 0.0, "max": 0}


def _bars_ok(df: pd.DataFrame, required: int, *, label: str) -> bool:
    try:
        prof = _bars_profile(df)
        counts = prof.get("counts")

        if counts is None or len(counts) == 0:
            logger.info(
                "[summary_recovery] dense 1m warmup check label=%s required=%s symbols=0 short=0 ok=0 min=0 median=0 max=0",
                label,
                int(required),
            )
            return False

        required = int(required)
        short = int((counts < required).sum())
        ok = int((counts >= required).sum())

        logger.info(
            "[summary_recovery] dense 1m warmup check label=%s required=%s symbols=%s short=%s ok=%s min=%s median=%.1f max=%s",
            label,
            required,
            int(counts.shape[0]),
            short,
            ok,
            int(counts.min()),
            float(counts.median()),
            int(counts.max()),
        )
        return short == 0
    except Exception:
        logger.exception("[summary_recovery] dense 1m bars_ok check failed label=%s", label)
        return False


def _keep_tail_per_symbol(df: pd.DataFrame, bars_per_symbol: int) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        if "symbol" not in df.columns or "datetime" not in df.columns:
            return df.copy().reset_index(drop=True)

        x = df.copy()
        x["symbol"] = x["symbol"].astype(str)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"])

        if x.empty:
            return pd.DataFrame()

        keep_bars = max(int(bars_per_symbol), FULL_FALLBACK_BARS_1M)

        x = (
            x.sort_values(["symbol", "datetime"], kind="stable")
            .groupby("symbol", group_keys=False)
            .tail(keep_bars)
            .reset_index(drop=True)
        )
        return x
    except Exception:
        logger.debug("[summary_recovery] keep tail per symbol failed", exc_info=True)
        return df


def _dedupe_symbol_datetime_keep_last(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        if "symbol" not in df.columns or "datetime" not in df.columns:
            return df.copy().reset_index(drop=True)

        x = df.copy()
        x["symbol"] = x["symbol"].astype(str)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"])

        if x.empty:
            return pd.DataFrame()

        x = (
            x.sort_values(["symbol", "datetime"], kind="stable")
            .drop_duplicates(subset=["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )
        return x
    except Exception:
        logger.debug("[summary_recovery] dedupe symbol/datetime failed", exc_info=True)
        return df


def _filter_symbols(df: pd.DataFrame, symbols: list[str] | None) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        symbols = normalize_symbols(symbols or [])
        if not symbols or "symbol" not in df.columns:
            return df.copy().reset_index(drop=True)

        x = df.copy()
        x["symbol"] = x["symbol"].astype(str)
        return x.loc[x["symbol"].isin(symbols)].copy().reset_index(drop=True)
    except Exception:
        logger.debug("[summary_recovery] filter symbols failed", exc_info=True)
        return df


def _filter_max_allowed_dt(df: pd.DataFrame, max_allowed_dt) -> pd.DataFrame:
    try:
        if df is None or df.empty or max_allowed_dt is None or pd.isna(max_allowed_dt):
            return df
        if "datetime" not in df.columns:
            return df

        x = df.copy()
        dt_s = pd.to_datetime(x["datetime"], errors="coerce")
        max_ts = pd.to_datetime(max_allowed_dt, errors="coerce")
        return x.loc[dt_s <= max_ts].copy().reset_index(drop=True)
    except Exception:
        logger.debug("[summary_recovery] max_allowed_dt filter failed", exc_info=True)
        return df


def _rebuild_dense_1m_from_push_source(
    *,
    delta_symbols: list[str],
    dates,
    max_allowed_dt,
    required_bars: int,
    runtime_push: pd.DataFrame | None,
) -> pd.DataFrame:
    try:
        push_src = runtime_push.copy() if isinstance(runtime_push, pd.DataFrame) and not runtime_push.empty else pd.DataFrame()

        if push_src.empty:
            push_src = load_push_df_for_dates(
                dates,
                drop_future_ticks=True,
                market_hours_only=False,
            )

        if push_src is None or push_src.empty:
            logger.warning("[summary_recovery] push source empty for dense 1m rebuild supplement")
            return pd.DataFrame()

        push_src = _filter_symbols(push_src, delta_symbols)

        if push_src.empty:
            logger.warning(
                "[summary_recovery] push source filtered empty for delta symbols count=%d",
                len(delta_symbols or []),
            )
            return pd.DataFrame()

        rebuilt_1m = rebuild_1min_from_push(push_src)
        rebuilt_1m = normalize_datetime_columns(rebuilt_1m, interval=1)
        rebuilt_1m = _filter_symbols(rebuilt_1m, delta_symbols)
        rebuilt_1m = _filter_max_allowed_dt(rebuilt_1m, max_allowed_dt)
        rebuilt_1m = _dedupe_symbol_datetime_keep_last(rebuilt_1m)
        rebuilt_1m = _keep_tail_per_symbol(rebuilt_1m, required_bars)

        if rebuilt_1m.empty:
            logger.warning("[summary_recovery] rebuilt 1m from push supplement empty")
            return pd.DataFrame()

        logger.info(
            "[summary_recovery] dense 1m rebuilt from push supplement rows=%d symbols=%d required_bars=%d",
            len(rebuilt_1m),
            _safe_symbol_count(rebuilt_1m),
            required_bars,
        )
        log_source_date_breakdown(
            rebuilt_1m,
            label="dense_1m_from_push_rebuild_supplement",
            target_dates_ctx=dates,
            anchor_day=None,
            required_bars_per_symbol=required_bars,
        )
        return rebuilt_1m

    except Exception:
        logger.exception("[summary_recovery] dense 1m rebuild from push supplement failed")
        return pd.DataFrame()


def _merge_dense_tail_and_push_supplement(
    *,
    tail_1m: pd.DataFrame,
    push_1m: pd.DataFrame,
    required_bars: int,
    dates,
    anchor_day,
) -> pd.DataFrame:
    try:
        has_tail = isinstance(tail_1m, pd.DataFrame) and not tail_1m.empty
        has_push = isinstance(push_1m, pd.DataFrame) and not push_1m.empty

        if not has_tail and not has_push:
            return pd.DataFrame()

        if has_tail and not has_push:
            merged = tail_1m.copy()
        elif has_push and not has_tail:
            merged = push_1m.copy()
        else:
            merged = merge_summary_frames_with_priority(tail_1m, push_1m, interval=1)

        merged = normalize_datetime_columns(merged, interval=1)
        merged = _dedupe_symbol_datetime_keep_last(merged)
        merged = _keep_tail_per_symbol(merged, required_bars)

        logger.info(
            "[summary_recovery] dense 1m merged tail+push rows=%d symbols=%d tail_rows=%d push_rows=%d required_bars=%d",
            len(merged),
            _safe_symbol_count(merged),
            len(tail_1m) if isinstance(tail_1m, pd.DataFrame) else 0,
            len(push_1m) if isinstance(push_1m, pd.DataFrame) else 0,
            int(required_bars),
        )

        _bars_ok(merged, required_bars, label="dense_1m_tail_plus_push")

        log_source_date_breakdown(
            merged,
            label="dense_1m_tail_plus_push",
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            required_bars_per_symbol=required_bars,
        )
        return merged

    except Exception:
        logger.exception("[summary_recovery] dense 1m tail+push merge failed")
        if isinstance(tail_1m, pd.DataFrame) and not tail_1m.empty:
            return tail_1m
        if isinstance(push_1m, pd.DataFrame) and not push_1m.empty:
            return push_1m
        return pd.DataFrame()


def _load_full_fallback_1m_history(
    *,
    dates,
    anchor_day,
    max_allowed_dt,
    required_bars: int,
    last_1m_dt,
) -> pd.DataFrame:
    try:
        bars_to_load = max(int(required_bars), FULL_FALLBACK_BARS_1M)

        df = load_recent_1m_history_for_recalc(
            last_1m_dt,
            dates=dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
            min_bars=bars_to_load,
        )

        df = normalize_datetime_columns(df, interval=1)
        df = _filter_max_allowed_dt(df, max_allowed_dt)
        df = _dedupe_symbol_datetime_keep_last(df)
        df = _keep_tail_per_symbol(df, bars_to_load)

        logger.warning(
            "[summary_recovery] full 1m fallback loaded rows=%d symbols=%d bars_to_load=%d",
            len(df),
            _safe_symbol_count(df),
            bars_to_load,
        )

        log_source_date_breakdown(
            df,
            label="dense_1m_full_fallback",
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            required_bars_per_symbol=bars_to_load,
        )
        _bars_ok(df, required_bars, label="dense_1m_full_fallback")

        return df

    except Exception:
        logger.exception("[summary_recovery] full 1m fallback load failed")
        return pd.DataFrame()


def _load_dense_1m_history_for_delta_symbols(
    *,
    delta_symbols: list[str],
    last_1m_dt,
    dates,
    anchor_day,
    max_allowed_dt,
    required_bars: int,
    runtime_push: pd.DataFrame | None = None,
) -> pd.DataFrame:
    delta_symbols = normalize_symbols(delta_symbols)
    dense_tail = pd.DataFrame()

    if delta_symbols:
        try:
            end_dt = last_1m_dt if last_1m_dt is not None else max_allowed_dt

            dense_tail = load_src_1m_tail_for_symbols(
                symbols=delta_symbols,
                bars_per_symbol=required_bars,
                end_dt=end_dt,
                target_dates_ctx=dates,
                anchor_day=anchor_day,
                max_allowed_dt=max_allowed_dt,
            )
            dense_tail = normalize_datetime_columns(dense_tail, interval=1)
            dense_tail = _filter_symbols(dense_tail, delta_symbols)
            dense_tail = _filter_max_allowed_dt(dense_tail, max_allowed_dt)
            dense_tail = _dedupe_symbol_datetime_keep_last(dense_tail)
            dense_tail = _keep_tail_per_symbol(dense_tail, required_bars)

            if isinstance(dense_tail, pd.DataFrame) and not dense_tail.empty:
                logger.info(
                    "[summary_recovery] dense 1m history loaded from summary tail rows=%d symbols=%d required_bars=%d",
                    len(dense_tail),
                    _safe_symbol_count(dense_tail),
                    required_bars,
                )
                log_source_date_breakdown(
                    dense_tail,
                    label="dense_1m_from_summary_tail",
                    target_dates_ctx=dates,
                    anchor_day=anchor_day,
                    required_bars_per_symbol=required_bars,
                )

                if _bars_ok(dense_tail, required_bars, label="dense_1m_summary_tail"):
                    return dense_tail

                logger.warning(
                    "[summary_recovery] summary tail warmup insufficient -> merge push rebuild supplement required_bars=%d",
                    required_bars,
                )
            else:
                logger.warning("[summary_recovery] dense 1m summary tail empty -> use push rebuild supplement")
        except Exception:
            logger.exception("[summary_recovery] dense 1m history load failed for summary tail")
            dense_tail = pd.DataFrame()

    push_rebuilt = _rebuild_dense_1m_from_push_source(
        delta_symbols=delta_symbols,
        dates=dates,
        max_allowed_dt=max_allowed_dt,
        required_bars=required_bars,
        runtime_push=runtime_push,
    )

    merged = _merge_dense_tail_and_push_supplement(
        tail_1m=dense_tail,
        push_1m=push_rebuilt,
        required_bars=required_bars,
        dates=dates,
        anchor_day=anchor_day,
    )

    if not merged.empty and _bars_ok(merged, required_bars, label="dense_1m_tail_plus_push_final"):
        return merged

    logger.warning(
        "[summary_recovery] dense 1m history still insufficient after tail+push merge -> full fallback"
    )

    fallback = _load_full_fallback_1m_history(
        dates=dates,
        anchor_day=anchor_day,
        max_allowed_dt=max_allowed_dt,
        required_bars=required_bars,
        last_1m_dt=last_1m_dt,
    )
    if not fallback.empty:
        return fallback

    if merged.empty:
        logger.warning("[summary_recovery] dense 1m history unavailable after tail+push merge and fallback")

    return merged


def _auto_startup_delta_only(
    *,
    startup_delta_only: bool,
    last_1m_dt,
    last_3m_dt,
    last_5m_dt,
) -> bool:
    try:
        if not bool(startup_delta_only):
            return False

        ready = (
            last_1m_dt is not None and not pd.isna(last_1m_dt)
            and last_3m_dt is not None and not pd.isna(last_3m_dt)
            and last_5m_dt is not None and not pd.isna(last_5m_dt)
        )
        return bool(ready)
    except Exception:
        return bool(startup_delta_only)


# ============================================================
# main orchestrator
# ============================================================

def bootstrap_incremental_rebuild_from_push(
    *,
    include_previous_business_day: bool = False,
    persist: bool = True,
    update_cache: bool = True,
    warmup_bars_3m: int = 90,
    warmup_bars_5m: int = 90,
    startup_delta_only: bool = True,
) -> dict:
    result = {
        "loaded_1m": 0,
        "loaded_3m": 0,
        "loaded_5m": 0,
        "push_rows": 0,
        "delta_push_rows": 0,
        "delta_1m_rows": 0,
        "delta_3m_rows": 0,
        "delta_5m_rows": 0,
        "delta_dates": [],
        "delta_symbols": 0,
        "used_explicit_delta_guard": False,
        "used_previous_business_day": False,
        "used_small_delta_path": False,
        "last_1m_dt": None,
        "last_3m_dt": None,
        "last_5m_dt": None,
        "src_1m_3m_rows": 0,
        "src_1m_5m_rows": 0,
        "startup_delta_only": bool(startup_delta_only),
        "ok": False,
        "skipped": False,
        "skip_reason": None,
        "summary_1min": pd.DataFrame(),
        "summary_3min": pd.DataFrame(),
        "summary_5min": pd.DataFrame(),
    }

    try:
        market_open_today = is_today_business_day()

        initial_dates = target_dates(include_previous_business_day=False)
        anchor_day, max_allowed_dt = resolve_anchor_context(initial_dates)

        logger.info(
            "[summary_recovery] bootstrap(delta-only=%s) start business_day=%s requested_persist=%s update_cache=%s initial_dates=%s anchor_day=%s max_allowed_dt=%s",
            startup_delta_only,
            market_open_today,
            persist,
            update_cache,
            [str(x) for x in initial_dates],
            anchor_day,
            max_allowed_dt,
        )

        last_1m_dt = load_last_summary_datetime_compat(
            1,
            target_dates_ctx=initial_dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )
        last_3m_dt = load_last_summary_datetime_compat(
            3,
            target_dates_ctx=initial_dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )
        last_5m_dt = load_last_summary_datetime_compat(
            5,
            target_dates_ctx=initial_dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )

        startup_delta_only = _auto_startup_delta_only(
            startup_delta_only=startup_delta_only,
            last_1m_dt=last_1m_dt,
            last_3m_dt=last_3m_dt,
            last_5m_dt=last_5m_dt,
        )
        result["startup_delta_only"] = bool(startup_delta_only)

        use_previous_business_day = _should_include_previous_business_day(
            requested=include_previous_business_day,
            startup_delta_only=startup_delta_only,
            last_1m_dt=last_1m_dt,
            last_3m_dt=last_3m_dt,
            last_5m_dt=last_5m_dt,
            market_open_today=market_open_today,
        )

        dates = target_dates(include_previous_business_day=use_previous_business_day)
        result["used_previous_business_day"] = bool(use_previous_business_day)

        if use_previous_business_day:
            anchor_day, max_allowed_dt = resolve_anchor_context(dates)

            last_1m_dt = load_last_summary_datetime_compat(
                1,
                target_dates_ctx=dates,
                anchor_day=anchor_day,
                max_allowed_dt=max_allowed_dt,
            )
            last_3m_dt = load_last_summary_datetime_compat(
                3,
                target_dates_ctx=dates,
                anchor_day=anchor_day,
                max_allowed_dt=max_allowed_dt,
            )
            last_5m_dt = load_last_summary_datetime_compat(
                5,
                target_dates_ctx=dates,
                anchor_day=anchor_day,
                max_allowed_dt=max_allowed_dt,
            )

            startup_delta_only = _auto_startup_delta_only(
                startup_delta_only=startup_delta_only,
                last_1m_dt=last_1m_dt,
                last_3m_dt=last_3m_dt,
                last_5m_dt=last_5m_dt,
            )
            result["startup_delta_only"] = bool(startup_delta_only)

        result["last_1m_dt"] = str(last_1m_dt) if last_1m_dt is not None else None
        result["last_3m_dt"] = str(last_3m_dt) if last_3m_dt is not None else None
        result["last_5m_dt"] = str(last_5m_dt) if last_5m_dt is not None else None

        effective_persist = bool(persist and market_open_today)

        logger.info(
            "[summary_recovery] checkpoints last_1m_dt=%s last_3m_dt=%s last_5m_dt=%s anchor_day=%s dates=%s use_prev_bday=%s effective_persist=%s startup_delta_only=%s",
            last_1m_dt,
            last_3m_dt,
            last_5m_dt,
            anchor_day,
            [str(x) for x in dates],
            use_previous_business_day,
            effective_persist,
            startup_delta_only,
        )

        runtime_push = load_runtime_push_delta_df(
            last_dt=last_1m_dt,
            allow_db_fallback=True,
            drop_future_ticks=True,
            market_hours_only=False,
            warmup_minutes=10,
        )

        if runtime_push.empty:
            runtime_push = load_runtime_push_df(
                allow_db_fallback=False,
                drop_future_ticks=True,
                market_hours_only=False,
            )

            if runtime_push.empty:
                runtime_push = load_push_df_for_dates(
                    dates,
                    drop_future_ticks=True,
                    market_hours_only=False,
                )

        result["push_rows"] = len(runtime_push)

        delta_push = filter_push_after(runtime_push, last_1m_dt)
        result["delta_push_rows"] = len(delta_push)

        if not delta_push.empty and "tick_time" in delta_push.columns:
            delta_dates = extract_dates_from_datetime_like(delta_push["tick_time"])
        else:
            delta_dates = []

        result["delta_dates"] = [str(x) for x in delta_dates]

        delta_symbols = _infer_delta_symbols(delta_push)
        result["delta_symbols"] = len(delta_symbols)

        logger.info(
            "[summary_recovery] delta push prepared push_rows=%d delta_push_rows=%d delta_symbols=%d delta_dates=%s",
            result["push_rows"],
            result["delta_push_rows"],
            result["delta_symbols"],
            result["delta_dates"],
        )

        if can_skip_rebuild_when_delta_empty(
            delta_push_empty=delta_push.empty,
            startup_delta_only=startup_delta_only,
            last_1m_dt=last_1m_dt,
            last_3m_dt=last_3m_dt,
            last_5m_dt=last_5m_dt,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        ):
            logger.info(
                "[summary_recovery] delta push empty and checkpoints fresh -> skip rebuild, restore persisted summaries only"
            )

            restored_1m, restored_3m, restored_5m = run_skip_rebuild_restore_path(
                update_cache=update_cache,
                last_1m_dt=last_1m_dt,
                last_3m_dt=last_3m_dt,
                last_5m_dt=last_5m_dt,
                dates=dates,
                anchor_day=anchor_day,
                max_allowed_dt=max_allowed_dt,
                warmup_bars_3m=warmup_bars_3m,
                warmup_bars_5m=warmup_bars_5m,
                SNAPSHOT_PRELOAD_MIN_BARS_1M=SNAPSHOT_PRELOAD_MIN_BARS_1M,
                SNAPSHOT_PRELOAD_MIN_BARS_3M=SNAPSHOT_PRELOAD_MIN_BARS_3M,
                SNAPSHOT_PRELOAD_MIN_BARS_5M=SNAPSHOT_PRELOAD_MIN_BARS_5M,
                SNAPSHOT_PRELOAD_BUFFER_BARS_1M=SNAPSHOT_PRELOAD_BUFFER_BARS_1M,
                SNAPSHOT_PRELOAD_BUFFER_BARS_3M=SNAPSHOT_PRELOAD_BUFFER_BARS_3M,
                SNAPSHOT_PRELOAD_BUFFER_BARS_5M=SNAPSHOT_PRELOAD_BUFFER_BARS_5M,
            )

            result["loaded_1m"] = len(restored_1m)
            result["loaded_3m"] = len(restored_3m)
            result["loaded_5m"] = len(restored_5m)
            result["summary_1min"] = restored_1m if isinstance(restored_1m, pd.DataFrame) else pd.DataFrame()
            result["summary_3min"] = restored_3m if isinstance(restored_3m, pd.DataFrame) else pd.DataFrame()
            result["summary_5min"] = restored_5m if isinstance(restored_5m, pd.DataFrame) else pd.DataFrame()
            result["ok"] = True
            result["skipped"] = True
            result["skip_reason"] = "delta_empty_and_checkpoints_fresh"

            logger.info("[summary_recovery] bootstrap done (skip rebuild) result=%s", result)
            return result

        if delta_push.empty and startup_delta_only:
            logger.info(
                "[summary_recovery] bootstrap delta push empty -> restore latest snapshots / recent tails"
            )

            snap_1m, snap_3m, snap_5m = _restore_latest_snapshots_if_available(update_cache=update_cache)

            if snap_1m.empty:
                snap_1m = _safe_load_recent_tail_default(
                    1,
                    end_dt=max_allowed_dt,
                    target_dates=dates,
                    anchor_day=anchor_day,
                    max_allowed_dt=max_allowed_dt,
                )
            if snap_3m.empty:
                snap_3m = _safe_load_recent_tail_default(
                    3,
                    end_dt=max_allowed_dt,
                    target_dates=dates,
                    anchor_day=anchor_day,
                    max_allowed_dt=max_allowed_dt,
                )
            if snap_5m.empty:
                snap_5m = _safe_load_recent_tail_default(
                    5,
                    end_dt=max_allowed_dt,
                    target_dates=dates,
                    anchor_day=anchor_day,
                    max_allowed_dt=max_allowed_dt,
                )

            if update_cache:
                for tf, df in ((1, snap_1m), (3, snap_3m), (5, snap_5m)):
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        _safe_update_cache(tf, df)

            result["loaded_1m"] = len(snap_1m)
            result["loaded_3m"] = len(snap_3m)
            result["loaded_5m"] = len(snap_5m)
            result["summary_1min"] = snap_1m
            result["summary_3min"] = snap_3m
            result["summary_5min"] = snap_5m
            result["ok"] = True
            result["skipped"] = True
            result["skip_reason"] = "delta_empty_restore_snapshot"

            logger.info("[summary_recovery] bootstrap done (snapshot restore) result=%s", result)
            return result

        delta_1m_ohlcv_raw = rebuild_1min_from_push(delta_push)
        delta_1m_ohlcv_raw = normalize_datetime_columns(delta_1m_ohlcv_raw, interval=1)
        delta_1m_ohlcv_raw = _dedupe_symbol_datetime_keep_last(delta_1m_ohlcv_raw)

        required_1m_bars = SNAPSHOT_PRELOAD_MIN_BARS_1M + SNAPSHOT_PRELOAD_BUFFER_BARS_1M

        hist_1m_raw = _load_dense_1m_history_for_delta_symbols(
            delta_symbols=delta_symbols,
            last_1m_dt=last_1m_dt,
            dates=dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
            required_bars=required_1m_bars,
            runtime_push=runtime_push,
        )

        full_1m_raw = merge_summary_frames_with_priority(hist_1m_raw, delta_1m_ohlcv_raw, interval=1)
        full_1m_raw = normalize_datetime_columns(full_1m_raw, interval=1)
        full_1m_raw = _dedupe_symbol_datetime_keep_last(full_1m_raw)
        full_1m_raw = _keep_tail_per_symbol(full_1m_raw, required_1m_bars)

        logger.info(
            "[summary_recovery] before indicators bootstrap_1m rows=%d symbols=%d latest_dt=%s required_1m_bars=%d",
            len(full_1m_raw),
            _safe_symbol_count(full_1m_raw),
            str(pd.to_datetime(full_1m_raw["datetime"], errors="coerce").max()) if isinstance(full_1m_raw, pd.DataFrame) and not full_1m_raw.empty and "datetime" in full_1m_raw.columns else None,
            required_1m_bars,
        )

        log_source_date_breakdown(
            full_1m_raw,
            label="full_1m_raw_before_indicators",
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            required_bars_per_symbol=required_1m_bars,
        )
        _bars_ok(full_1m_raw, required_1m_bars, label="full_1m_raw_before_indicators")

        full_1m_raw = apply_indicators_and_scoring_tail(
            full_1m_raw,
            interval=1,
            label="bootstrap_1m",
            tail_bars=450,
            safety_margin=20,
        )
        full_1m_raw = normalize_datetime_columns(full_1m_raw, interval=1)
        full_1m_raw = _dedupe_symbol_datetime_keep_last(full_1m_raw)

        logger.info(
            "[summary_recovery] after indicators bootstrap_1m rows=%d symbols=%d latest_dt=%s",
            len(full_1m_raw),
            _safe_symbol_count(full_1m_raw),
            str(pd.to_datetime(full_1m_raw["datetime"], errors="coerce").max()) if isinstance(full_1m_raw, pd.DataFrame) and not full_1m_raw.empty and "datetime" in full_1m_raw.columns else None,
        )

        log_source_date_breakdown(
            full_1m_raw,
            label="full_1m_raw_after_indicators",
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            required_bars_per_symbol=required_1m_bars,
        )
        _bars_ok(full_1m_raw, required_1m_bars, label="full_1m_raw_after_indicators")

        delta_1m_raw = full_1m_raw.copy()

        if last_1m_dt is not None and not delta_1m_raw.empty and "datetime" in delta_1m_raw.columns:
            dt_s = pd.to_datetime(delta_1m_raw["datetime"], errors="coerce")
            delta_1m_raw = delta_1m_raw.loc[dt_s > pd.to_datetime(last_1m_dt, errors="coerce")].copy()

        delta_1m_raw = normalize_datetime_columns(delta_1m_raw, interval=1)
        delta_1m_raw = _dedupe_symbol_datetime_keep_last(delta_1m_raw)

        delta_1m = finalize_for_upsert(delta_1m_raw, 1)
        result["delta_1m_rows"] = len(delta_1m)

        logger.info("[summary_recovery] delta_1m built rows=%d", result["delta_1m_rows"])

        if effective_persist and not delta_1m.empty:
            upsert_summary_df(delta_1m, 1)

        if update_cache and not delta_1m.empty:
            _safe_update_cache(1, delta_1m)

        now_dt = None

        if not delta_push.empty and "tick_time" in delta_push.columns:
            try:
                now_dt = pd.to_datetime(delta_push["tick_time"], errors="coerce").max()
            except Exception:
                now_dt = None

        if now_dt is None or pd.isna(now_dt):
            now_dt = max_allowed_dt if max_allowed_dt is not None else pd.Timestamp.now().tz_localize(None)

        bars_1m_for_3m = _bars_needed_for_higher_tf(3, warmup_bars_3m)
        bars_1m_for_5m = _bars_needed_for_higher_tf(5, warmup_bars_5m)

        start_3m, end_3m = calc_higher_tf_source_window(
            interval=3,
            last_higher_dt=last_3m_dt,
            now_dt=now_dt,
            warmup_bars=warmup_bars_3m,
        )
        start_5m, end_5m = calc_higher_tf_source_window(
            interval=5,
            last_higher_dt=last_5m_dt,
            now_dt=now_dt,
            warmup_bars=warmup_bars_5m,
        )

        start_3m = clamp_start_dt_to_recent_session(start_3m, now_dt)
        start_5m = clamp_start_dt_to_recent_session(start_5m, now_dt)

        small_delta_path = bool(
            startup_delta_only and len(delta_push) <= SMALL_DELTA_THRESHOLD_ROWS and len(delta_symbols) > 0
        )
        result["used_small_delta_path"] = small_delta_path

        if small_delta_path:
            logger.info(
                "[summary_recovery] small-delta path enabled rows=%d symbols=%d",
                len(delta_push),
                len(delta_symbols),
            )

        src_1m_for_3m_raw = load_src_1m_tail_for_symbols(
            symbols=delta_symbols if delta_symbols else None,
            bars_per_symbol=bars_1m_for_3m,
            end_dt=end_3m,
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )
        src_1m_for_5m_raw = load_src_1m_tail_for_symbols(
            symbols=delta_symbols if delta_symbols else None,
            bars_per_symbol=bars_1m_for_5m,
            end_dt=end_5m,
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )

        src_1m_for_3m_raw = normalize_datetime_columns(src_1m_for_3m_raw, interval=1)
        src_1m_for_5m_raw = normalize_datetime_columns(src_1m_for_5m_raw, interval=1)

        src_1m_for_3m_raw = merge_summary_frames_with_priority(src_1m_for_3m_raw, full_1m_raw, interval=1)
        src_1m_for_5m_raw = merge_summary_frames_with_priority(src_1m_for_5m_raw, full_1m_raw, interval=1)

        src_1m_for_3m_raw = normalize_datetime_columns(src_1m_for_3m_raw, interval=1)
        src_1m_for_5m_raw = normalize_datetime_columns(src_1m_for_5m_raw, interval=1)

        src_1m_for_3m_raw = _dedupe_symbol_datetime_keep_last(src_1m_for_3m_raw)
        src_1m_for_5m_raw = _dedupe_symbol_datetime_keep_last(src_1m_for_5m_raw)

        src_1m_for_3m_raw = _filter_df_from_start_dt(
            src_1m_for_3m_raw,
            start_3m,
            label="src_1m_for_3m_start_dt",
        )
        src_1m_for_5m_raw = _filter_df_from_start_dt(
            src_1m_for_5m_raw,
            start_5m,
            label="src_1m_for_5m_start_dt",
        )

        if startup_delta_only and result["delta_dates"]:
            try:
                explicit_dates = [pd.to_datetime(x).date() for x in result["delta_dates"]]

                src_1m_for_3m_raw = drop_rows_to_explicit_dates(
                    src_1m_for_3m_raw,
                    explicit_dates=explicit_dates,
                    label="src_1m_for_3m_explicit_dates",
                )
                src_1m_for_5m_raw = drop_rows_to_explicit_dates(
                    src_1m_for_5m_raw,
                    explicit_dates=explicit_dates,
                    label="src_1m_for_5m_explicit_dates",
                )

                result["used_explicit_delta_guard"] = True
            except Exception:
                logger.debug("[summary_recovery] explicit delta date guard failed", exc_info=True)

        log_source_date_breakdown(
            src_1m_for_3m_raw,
            label="src_1m_for_3m_after_merge",
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            required_bars_per_symbol=bars_1m_for_3m,
        )
        log_source_date_breakdown(
            src_1m_for_5m_raw,
            label="src_1m_for_5m_after_merge",
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            required_bars_per_symbol=bars_1m_for_5m,
        )

        result["src_1m_3m_rows"] = len(src_1m_for_3m_raw)
        result["src_1m_5m_rows"] = len(src_1m_for_5m_raw)

        rebuilt_3m_raw = rebuild_higher_tf_from_1m(src_1m_for_3m_raw, 3)
        rebuilt_5m_raw = rebuild_higher_tf_from_1m(src_1m_for_5m_raw, 5)

        rebuilt_3m_raw = normalize_datetime_columns(rebuilt_3m_raw, interval=3)
        rebuilt_5m_raw = normalize_datetime_columns(rebuilt_5m_raw, interval=5)

        rebuilt_3m_raw = apply_indicators_and_scoring_tail(
            rebuilt_3m_raw,
            interval=3,
            label="bootstrap_3m",
            tail_bars=140,
            safety_margin=10,
        )
        rebuilt_5m_raw = apply_indicators_and_scoring_tail(
            rebuilt_5m_raw,
            interval=5,
            label="bootstrap_5m",
            tail_bars=100,
            safety_margin=10,
        )

        rebuilt_3m_raw = normalize_datetime_columns(rebuilt_3m_raw, interval=3)
        rebuilt_5m_raw = normalize_datetime_columns(rebuilt_5m_raw, interval=5)

        if last_3m_dt is not None and not rebuilt_3m_raw.empty and "datetime" in rebuilt_3m_raw.columns:
            rebuilt_3m_raw = keep_newer_or_equal_last_bar(rebuilt_3m_raw, last_3m_dt, interval=3)

        if last_5m_dt is not None and not rebuilt_5m_raw.empty and "datetime" in rebuilt_5m_raw.columns:
            rebuilt_5m_raw = keep_newer_or_equal_last_bar(rebuilt_5m_raw, last_5m_dt, interval=5)

        delta_3m = finalize_for_upsert(rebuilt_3m_raw, 3)
        delta_5m = finalize_for_upsert(rebuilt_5m_raw, 5)

        delta_3m = limit_recent_tf_rows(delta_3m, interval=3, keep_bars=DELTA_KEEP_RECENT_BARS_3M)
        delta_5m = limit_recent_tf_rows(delta_5m, interval=5, keep_bars=DELTA_KEEP_RECENT_BARS_5M)

        result["delta_3m_rows"] = len(delta_3m)
        result["delta_5m_rows"] = len(delta_5m)

        logger.info(
            "[summary_recovery] higher-tf built delta_3m=%d delta_5m=%d src_1m_3m=%d src_1m_5m=%d",
            result["delta_3m_rows"],
            result["delta_5m_rows"],
            result["src_1m_3m_rows"],
            result["src_1m_5m_rows"],
        )

        if effective_persist and not delta_3m.empty:
            upsert_summary_df(delta_3m, 3)

        if effective_persist and not delta_5m.empty:
            upsert_summary_df(delta_5m, 5)

        if update_cache:
            if not delta_3m.empty:
                _safe_update_cache(3, delta_3m)
            if not delta_5m.empty:
                _safe_update_cache(5, delta_5m)

        cache_1m = pd.DataFrame()
        try:
            cache_1m = build_cache_seed_with_recent_history(
                interval=1,
                latest_df=delta_1m if not delta_1m.empty else full_1m_raw,
                target_dates=dates,
                anchor_day=anchor_day,
                max_allowed_dt=max_allowed_dt,
                symbols=delta_symbols if delta_symbols else None,
            )

            if update_cache and isinstance(cache_1m, pd.DataFrame) and not cache_1m.empty:
                _safe_update_cache(1, cache_1m)
        except Exception:
            logger.debug("[summary_recovery] cache seed build failed interval=1", exc_info=True)

        result["loaded_1m"] = len(delta_1m)
        result["loaded_3m"] = len(delta_3m)
        result["loaded_5m"] = len(delta_5m)
        result["summary_1min"] = (
            cache_1m if isinstance(cache_1m, pd.DataFrame) and not cache_1m.empty
            else (delta_1m if isinstance(delta_1m, pd.DataFrame) and not delta_1m.empty else full_1m_raw)
        )
        result["summary_3min"] = delta_3m if isinstance(delta_3m, pd.DataFrame) else pd.DataFrame()
        result["summary_5min"] = delta_5m if isinstance(delta_5m, pd.DataFrame) else pd.DataFrame()
        result["ok"] = True

        logger.info("[summary_recovery] bootstrap done result=%s", result)
        return result

    except Exception:
        logger.exception("[summary_recovery] bootstrap failed")
        return result