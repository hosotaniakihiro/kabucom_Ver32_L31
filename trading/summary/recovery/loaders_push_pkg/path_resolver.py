# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/path_resolver.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-PATH-RESOLVER
# ------------------------------------------------------------
# 【概要】
#   PUSH DB path / table name resolver
#
# 【主な機能】
#   ✔ global_data.push_db_dir 対応
#   ✔ default NAS path 対応
#   ✔ pushYYYYMMDD.db 解決
#   ✔ stream_data / push_data / ticks / push table 検出
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3

from global_state import global_data

from .constants import (
    DEFAULT_PUSH_DB_DIR,
    DEFAULT_PUSH_TABLE_CANDIDATES,
)

logger = logging.getLogger(__name__)


def resolve_push_db_path(trade_date: dt.date) -> str:
    date_str = trade_date.strftime("%Y%m%d")

    try:
        custom_dir = getattr(global_data, "push_db_dir", None)
        if custom_dir:
            path = os.path.join(custom_dir, f"push{date_str}.db")
            if os.path.exists(path):
                return path
    except Exception:
        logger.debug(
            "[summary.recovery.loaders_push.path_resolver] custom push_db_dir resolve failed",
            exc_info=True,
        )

    return os.path.join(DEFAULT_PUSH_DB_DIR, f"push{date_str}.db")


def detect_push_table_name(conn: sqlite3.Connection):
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        tables = {str(r[0]) for r in rows if r and r[0] is not None}

        for name in DEFAULT_PUSH_TABLE_CANDIDATES:
            if name in tables:
                return name

        if tables:
            return sorted(tables)[0]

    except Exception:
        logger.exception("[summary.recovery.loaders_push.path_resolver] failed detect push table")

    return None


__all__ = [
    "resolve_push_db_path",
    "detect_push_table_name",
]