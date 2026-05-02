# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/__init__.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-PACKAGE
# ------------------------------------------------------------
# 【概要】
#   loaders_push 分割パッケージの公開入口
#
# 【主な機能】
#   ✔ constants 公開
#   ✔ timezone helper 公開
#   ✔ SQL helper 公開
#   ✔ path/table detector 公開
#   ✔ normalizer 公開
#   ✔ filters 公開
#   ✔ DB loader 公開
#   ✔ runtime loader 公開
#   ✔ checkpoint filter 公開
#
# 【互換性】
#   旧 loaders_push.py から import * される前提。
# ============================================================

from __future__ import annotations

from .constants import (
    DEFAULT_PUSH_DB_DIR,
    DEFAULT_PUSH_TABLE_CANDIDATES,
    DEFAULT_SYMBOL_CHUNK_SIZE,
    MARKET_AM_START,
    MARKET_AM_END,
    MARKET_PM_START,
    MARKET_PM_END,
    PUSH_TIME_COLUMN_CANDIDATES,
    PUSH_SYMBOL_COLUMN_CANDIDATES,
)

from .timezone import (
    strip_tz_keep_wallclock,
    to_tz_naive_timestamp,
    to_tz_naive_datetime_series,
    format_sql_dt,
)

from .sql_helpers import (
    quote_ident,
    fetch_push_table_columns,
    build_push_time_where_clause,
)

from .path_resolver import (
    resolve_push_db_path,
    detect_push_table_name,
)

from .normalizer import (
    normalize_symbols,
    normalize_push_df,
)

from .filters import (
    filter_future_ticks,
    filter_market_session_ticks,
)

from .db_loader import (
    load_push_df_for_dates,
)

from .runtime_loader import (
    load_runtime_push_df,
    load_runtime_push_delta_df,
)

from .checkpoint import (
    filter_push_after,
)


# ------------------------------------------------------------
# Backward-compatible aliases
# ------------------------------------------------------------
_strip_tz_keep_wallclock = strip_tz_keep_wallclock
_to_tz_naive_timestamp = to_tz_naive_timestamp
_to_tz_naive_datetime_series = to_tz_naive_datetime_series
_format_sql_dt = format_sql_dt
_quote_ident = quote_ident
_filter_future_ticks = filter_future_ticks
_filter_market_session_ticks = filter_market_session_ticks


__all__ = [
    # constants
    "DEFAULT_PUSH_DB_DIR",
    "DEFAULT_PUSH_TABLE_CANDIDATES",
    "DEFAULT_SYMBOL_CHUNK_SIZE",
    "MARKET_AM_START",
    "MARKET_AM_END",
    "MARKET_PM_START",
    "MARKET_PM_END",
    "PUSH_TIME_COLUMN_CANDIDATES",
    "PUSH_SYMBOL_COLUMN_CANDIDATES",

    # timezone
    "strip_tz_keep_wallclock",
    "to_tz_naive_timestamp",
    "to_tz_naive_datetime_series",
    "format_sql_dt",

    # backward-compatible private aliases
    "_strip_tz_keep_wallclock",
    "_to_tz_naive_timestamp",
    "_to_tz_naive_datetime_series",
    "_format_sql_dt",

    # sql
    "quote_ident",
    "_quote_ident",
    "fetch_push_table_columns",
    "build_push_time_where_clause",

    # path/table
    "resolve_push_db_path",
    "detect_push_table_name",

    # normalizer
    "normalize_symbols",
    "normalize_push_df",

    # filters
    "filter_future_ticks",
    "filter_market_session_ticks",
    "_filter_future_ticks",
    "_filter_market_session_ticks",

    # loaders
    "load_push_df_for_dates",
    "load_runtime_push_df",
    "load_runtime_push_delta_df",

    # checkpoint
    "filter_push_after",
]