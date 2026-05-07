# ============================================================
# File   : database/sqlite/__init__.py
# Version: PRODUCTION-STABLE-REV1.2-NAS-IO-PATCH
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from .retry import (
    DEFAULT_BUSY_TIMEOUT_MS,
    is_lock_error,
    lock_sleep_seconds,
    normal_sleep_seconds,
    prepare_sqlite_connection,
    run_sql_many_with_retry,
)

try:
    from .retry_io_patch import install_retry_io_patch

    if install_retry_io_patch():
        from .retry_io_patch import (
            DEFAULT_BUSY_TIMEOUT_MS,
            is_lock_error,
            lock_sleep_seconds,
            normal_sleep_seconds,
            prepare_sqlite_connection,
            run_sql_many_with_retry,
        )
except Exception:
    logger.exception("[database.sqlite] retry_io_patch install failed")

from .normalize import (
    is_null_like,
    normalize_datetime_value,
    normalize_date_value,
    normalize_time_value,
    normalize_scalar_value,
    normalize_row_for_sqlite,
    normalize_rows_for_sqlite,
)
from .inspector import (
    read_table_columns,
    invalidate_table_columns_cache,
    table_has_unique_constraint,
    invalidate_constraint_cache,
)
from .sql_builder import (
    quote_ident,
    sqlite_quote_literal,
    build_sqlite_upsert_sql,
    build_insert_sql,
    build_delete_by_columns_sql,
)
from .wal import (
    CheckpointMode,
    get_wal_path,
    get_shm_path,
    get_file_size_bytes,
    get_sqlite_wal_status,
    checkpoint_sqlite_wal,
    checkpoint_sqlite_wal_if_large,
)

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "is_lock_error",
    "lock_sleep_seconds",
    "normal_sleep_seconds",
    "prepare_sqlite_connection",
    "run_sql_many_with_retry",
    "is_null_like",
    "normalize_datetime_value",
    "normalize_date_value",
    "normalize_time_value",
    "normalize_scalar_value",
    "normalize_row_for_sqlite",
    "normalize_rows_for_sqlite",
    "read_table_columns",
    "invalidate_table_columns_cache",
    "table_has_unique_constraint",
    "invalidate_constraint_cache",
    "quote_ident",
    "sqlite_quote_literal",
    "build_sqlite_upsert_sql",
    "build_insert_sql",
    "build_delete_by_columns_sql",
    "CheckpointMode",
    "get_wal_path",
    "get_shm_path",
    "get_file_size_bytes",
    "get_sqlite_wal_status",
    "checkpoint_sqlite_wal",
    "checkpoint_sqlite_wal_if_large",
]
