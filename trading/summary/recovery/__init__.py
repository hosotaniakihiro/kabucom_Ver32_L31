# ============================================================
# File   : trading/summary/recovery/__init__.py
# Ver    : PRODUCTION-STABLE-REV8.0-DELTA-FIRST-PACKAGE-MODULAR
# ------------------------------------------------------------
# 【概要】
#   trading.summary.recovery パッケージの公開入口
#
# 【主な機能】
#   - helpers / loaders / rebuilders / persistence の公開APIを再エクスポート
#   - guards / market_hours / preloaders / incremental_processors の公開APIを再エクスポート
#   - 呼び出し側の import を簡潔にする
#
# 【設計方針】
#   - 本ファイルは薄い re-export のみ
#   - 実ロジックは各モジュールへ分離
#   - engine 側は本 package から必要関数をまとめて import 可能
# ============================================================

from __future__ import annotations

from .helpers import (
    ensure_dataframe,
    safe_get_series,
    to_datetime_naive,
    looks_like_symbol_series,
    cleanup_symbol_series,
    normalize_symbol,
    coalesce_duplicate_columns,
    coalesce_first_numeric,
    repair_ohlc_alias,
    build_time_range_from_datetime,
    repair_datetime_from_time_range,
    normalize_datetime_columns,
    merge_summary_frames_with_priority,
    today,
    get_previous_business_day,
    is_today_business_day,
    target_dates,
    extract_dates_from_datetime_like,
    drop_rows_outside_allowed_dates,
    drop_rows_to_explicit_dates,
)

from .loaders import (
    DEFAULT_PUSH_DB_DIR,
    DEFAULT_PUSH_TABLE_CANDIDATES,
    resolve_summary_table_name_from_model,
    read_sqlalchemy_model_to_df,
    load_last_summary_datetime,
    load_summary_df_from_datetime,
    load_summary_df_between,
    resolve_compact_preload_start,
    resolve_push_db_path,
    detect_push_table_name,
    normalize_push_df,
    load_push_df_for_dates,
    load_runtime_push_df,
    filter_push_after,
)

from .rebuilders import (
    RECENT_RECALC_BARS_1M,
    calc_higher_tf_source_window,
    rebuild_1min_from_push,
    rebuild_higher_tf_from_1m,
    trim_recent_bars,
)

from .persistence import (
    finalize_for_upsert,
    upsert_summary_df,
    update_global_cache,
)

from .guards import (
    JST,
    AM_START,
    AM_END,
    PM_START,
    PM_END,
    now_jst_naive,
    floor_dt,
    normalize_dt_like,
    normalize_time_cols_for_guard,
    session_cap_for_now,
    clip_ts_to_cap,
    rebuild_time_range_from_cols,
    guard_future_rows,
)

from .market_hours import (
    market_close_time_for_interval,
    is_market_session_time,
    filter_market_hours_rows,
)

from .preloaders import (
    DELTA_SOURCE_SESSION_FLOOR_HOUR,
    history_window_by_interval,
    load_recent_history_for_cache,
    clamp_start_dt_to_recent_session,
    limit_recent_tf_rows,
    build_cache_seed_with_recent_history,
)

from .incremental_processors import (
    process_incremental_1m,
    process_incremental_higher_tf,
)
from .bootstrap_orchestrator import (
    bootstrap_incremental_rebuild_from_push,
)

from .checkpoints import (
    resolve_anchor_context,
    checkpoint_is_fresh,
    can_skip_rebuild_when_delta_empty,
)
__all__ = [
    # helpers
    "ensure_dataframe",
    "safe_get_series",
    "to_datetime_naive",
    "looks_like_symbol_series",
    "cleanup_symbol_series",
    "normalize_symbol",
    "coalesce_duplicate_columns",
    "coalesce_first_numeric",
    "repair_ohlc_alias",
    "build_time_range_from_datetime",
    "repair_datetime_from_time_range",
    "normalize_datetime_columns",
    "merge_summary_frames_with_priority",
    "today",
    "get_previous_business_day",
    "is_today_business_day",
    "target_dates",
    "extract_dates_from_datetime_like",
    "drop_rows_outside_allowed_dates",
    "drop_rows_to_explicit_dates",

    # loaders
    "DEFAULT_PUSH_DB_DIR",
    "DEFAULT_PUSH_TABLE_CANDIDATES",
    "resolve_summary_table_name_from_model",
    "read_sqlalchemy_model_to_df",
    "load_last_summary_datetime",
    "load_summary_df_from_datetime",
    "load_summary_df_between",
    "resolve_compact_preload_start",
    "resolve_push_db_path",
    "detect_push_table_name",
    "normalize_push_df",
    "load_push_df_for_dates",
    "load_runtime_push_df",
    "filter_push_after",

    # rebuilders
    "RECENT_RECALC_BARS_1M",
    "calc_higher_tf_source_window",
    "rebuild_1min_from_push",
    "rebuild_higher_tf_from_1m",
    "trim_recent_bars",

    # persistence
    "finalize_for_upsert",
    "upsert_summary_df",
    "update_global_cache",

    # guards
    "JST",
    "AM_START",
    "AM_END",
    "PM_START",
    "PM_END",
    "now_jst_naive",
    "floor_dt",
    "normalize_dt_like",
    "normalize_time_cols_for_guard",
    "session_cap_for_now",
    "clip_ts_to_cap",
    "rebuild_time_range_from_cols",
    "guard_future_rows",

    # market_hours
    "market_close_time_for_interval",
    "is_market_session_time",
    "filter_market_hours_rows",

    # preloaders
    "DELTA_SOURCE_SESSION_FLOOR_HOUR",
    "history_window_by_interval",
    "load_recent_history_for_cache",
    "clamp_start_dt_to_recent_session",
    "limit_recent_tf_rows",
    "build_cache_seed_with_recent_history",

    # incremental_processors
    "process_incremental_1m",
    "process_incremental_higher_tf",

    # checkpoints
    "resolve_anchor_context",
    "checkpoint_is_fresh",
    "can_skip_rebuild_when_delta_empty",

    # bootstrap orchestrator
    "bootstrap_incremental_rebuild_from_push",
]