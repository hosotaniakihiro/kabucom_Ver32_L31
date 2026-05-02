# ============================================================
# File   : trading/yahoo/pipeline/complement_pipeline.py
# Version: PRODUCTION-STABLE-REV4.1-COMPAT-SHIM
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完パイプラインの互換 shim
#
# 【目的】
#   旧 import パスを維持する:
#
#     from trading.yahoo.pipeline.complement_pipeline import ...
#
# 【実体】
#   実ロジックは以下へ分割済み:
#
#     trading/yahoo/pipeline/complement/
#       __init__.py
#       constants.py
#       db.py
#       normalize.py
#       resample.py
#       diff.py
#       compute.py
#       save.py
#       runner.py
#
# 【主な公開API】
#   - run_yahoo_summary_pipeline
#   - run_yahoo_mtf_summary_pipeline
#   - run_yahoo_complement_pipeline
#   - run_yahoo_complement_once
#   - get_latest_yahoo_summary_datetime
#
# 【source】
#   PUSH由来:
#     - summary_recovery_push_1m
#     - summary_recovery_resample_3m
#     - summary_recovery_resample_5m
#
#   Yahoo補完由来:
#     - summary_recovery_yahoo_1m
#     - summary_recovery_yahoo_resample_3m
#     - summary_recovery_yahoo_resample_5m
#
# 【重要】
#   - このファイルには実処理を書かない
#   - 既存 import を壊さないための re-export 専用
#   - 修正は基本的に trading.yahoo.pipeline.complement.* 側で行う
# ============================================================

from __future__ import annotations

from trading.yahoo.pipeline.complement import (
    DEFAULT_BASE_DIR,
    DEFAULT_INTERVALS,
    SUPPORTED_INTERVALS,
    SUMMARY_TABLE_BY_INTERVAL,
    PUSH_SUMMARY_SOURCE_BY_INTERVAL,
    YAHOO_SUMMARY_SOURCE_BY_INTERVAL,
    SUMMARY_SOURCE_BY_INTERVAL,
    PUSH_SOURCE_BY_INTERVAL,
    DEFAULT_WARMUP_MINUTES,
    DEFAULT_OVERLAP_MINUTES_BY_INTERVAL,
    DEFAULT_TOUCH_RECENT_MINUTES,
    YAHOO_SUMMARY_LOCK_TIMEOUT_SEC,
    YAHOO_SUMMARY_SKIP_IF_BUSY,
    normalize_interval,
    yahoo_source_for_interval,
    push_source_for_interval,
    summary_table_for_interval,
    today_yyyymmdd,
    today_date_str,
    resolve_base_dir,
    get_summary_db_path,
    get_latest_summary_datetime_by_source,
    get_latest_yahoo_summary_datetime,
    get_latest_push_summary_datetime,
    get_latest_any_summary_datetime,
    get_latest_datetimes_report,
    normalize_yahoo_1min_df,
    build_interval_frame,
    run_yahoo_summary_pipeline,
    run_yahoo_mtf_summary_pipeline,
    run_yahoo_complement_pipeline,
    run_yahoo_complement_once,
)

__all__ = [
    "DEFAULT_BASE_DIR",
    "DEFAULT_INTERVALS",
    "SUPPORTED_INTERVALS",
    "SUMMARY_TABLE_BY_INTERVAL",
    "PUSH_SUMMARY_SOURCE_BY_INTERVAL",
    "YAHOO_SUMMARY_SOURCE_BY_INTERVAL",
    "SUMMARY_SOURCE_BY_INTERVAL",
    "PUSH_SOURCE_BY_INTERVAL",
    "DEFAULT_WARMUP_MINUTES",
    "DEFAULT_OVERLAP_MINUTES_BY_INTERVAL",
    "DEFAULT_TOUCH_RECENT_MINUTES",
    "YAHOO_SUMMARY_LOCK_TIMEOUT_SEC",
    "YAHOO_SUMMARY_SKIP_IF_BUSY",
    "normalize_interval",
    "yahoo_source_for_interval",
    "push_source_for_interval",
    "summary_table_for_interval",
    "today_yyyymmdd",
    "today_date_str",
    "resolve_base_dir",
    "get_summary_db_path",
    "get_latest_summary_datetime_by_source",
    "get_latest_yahoo_summary_datetime",
    "get_latest_push_summary_datetime",
    "get_latest_any_summary_datetime",
    "get_latest_datetimes_report",
    "normalize_yahoo_1min_df",
    "build_interval_frame",
    "run_yahoo_summary_pipeline",
    "run_yahoo_mtf_summary_pipeline",
    "run_yahoo_complement_pipeline",
    "run_yahoo_complement_once",
]