# ============================================================
# File   : trading/summary/persistence/schema/schema_utils.py
# Version: Ver1.0-PRODUCTION-SCHEMA-UTILS-HARDENED
# ------------------------------------------------------------
# ✔ summary_saver_bulk 互換
# ✔ WALモード保証（SQLite）
# ✔ busy_timeout設定
# ✔ synchronous最適化
# ✔ 既存カラムのみフィルタ（安全）
# ✔ テーブル未作成時の安全処理
# ✔ SQLite / DuckDB 互換
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import List

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# WAL MODE（SQLite）
# ============================================================

def ensure_wal_mode(conn) -> None:

    try:

        # SQLiteのみ対象
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=5000")

        logger.debug("[SCHEMA] WAL mode ensured")

    except Exception:
        # DuckDBなどでは失敗するので握りつぶす
        logger.debug("[SCHEMA] WAL mode skip (non-sqlite)")


# ============================================================
# TABLE COLUMN FETCH
# ============================================================

def _get_table_columns(conn, table_name: str) -> List[str]:

    try:

        rows = conn.exec_driver_sql(
            f"PRAGMA table_info({table_name})"
        ).fetchall()

        if not rows:
            return []

        columns = [r[1] for r in rows]

        return columns

    except Exception:

        logger.exception(
            "[SCHEMA] failed to fetch table columns → %s",
            table_name
        )

        return []


# ============================================================
# FILTER EXISTING COLUMNS
# ============================================================

def filter_existing_columns(
    df: pd.DataFrame,
    conn,
    table_name: str
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        table_cols = _get_table_columns(conn, table_name)

        if not table_cols:

            logger.warning(
                "[SCHEMA] table not found or empty → %s",
                table_name
            )

            # テーブルがまだない場合はそのまま返す（create用）
            return df

        df_cols = df.columns.tolist()

        # ----------------------------------------------------
        # 共通カラム
        # ----------------------------------------------------
        common_cols = [c for c in df_cols if c in table_cols]

        removed_cols = [c for c in df_cols if c not in table_cols]

        if removed_cols:
            logger.warning(
                "[SCHEMA] dropped non-existing columns → %s",
                removed_cols
            )

        if not common_cols:
            logger.error(
                "[SCHEMA] no matching columns → skip insert"
            )
            return pd.DataFrame()

        df = df[common_cols]

        return df

    except Exception:

        logger.exception(
            "[SCHEMA] filter_existing_columns failed"
        )

        return pd.DataFrame()