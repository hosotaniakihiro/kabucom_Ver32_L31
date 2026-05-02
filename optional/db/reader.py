# ============================================================
# optional/db/reader.py
# Version: Ver2.0-PRODUCTION-SYMBOL-FLAGS-PRIMARY
# ------------------------------------------------------------
# ✔ symbol_flags.db を優先して読み込み（NEW）
# ✔ optional_master fallback
# ✔ SQLite接続安全化
# ✔ DB存在チェック
# ✔ テーブル存在チェック
# ✔ DataFrame保証
# ✔ symbol型安全化
# ✔ symbolname保証
# ✔ NaN / inf 防御
# ✔ paths.py利用
# ✔ 本番ログ
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import pandas as pd

from config.paths import get_path

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# DB PATH
# ------------------------------------------------------------

OPTIONAL_DB_PATH = get_path("optional_db")
SYMBOL_FLAGS_DB = get_path("symbol_flags_db")


# ============================================================
# Utility
# ============================================================

def _db_exists(path) -> bool:

    if not path:
        return False

    return os.path.exists(path)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:

    try:

        q = """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
        AND name=?
        """

        cur = conn.execute(q, (table,))
        row = cur.fetchone()

        return row is not None

    except Exception:

        logger.exception("[OPTIONAL READER] table check failed")

        return False


def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # --------------------------------------------------
    # symbol型保証
    # --------------------------------------------------

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)

    # --------------------------------------------------
    # symbolname保証
    # --------------------------------------------------

    if "symbolname" not in df.columns and "symbol" in df.columns:
        df["symbolname"] = df["symbol"]

    if "symbolname" in df.columns:
        df["symbolname"] = (
            df["symbolname"]
            .astype(str)
            .str.strip()
        )

    # --------------------------------------------------
    # NaN / inf 防御
    # --------------------------------------------------

    df = df.replace([float("inf"), float("-inf")], None)

    return df


# ============================================================
# symbol_flags loader（PRIMARY）
# ============================================================

def _load_from_symbol_flags() -> pd.DataFrame:

    try:

        if not _db_exists(SYMBOL_FLAGS_DB):

            logger.warning(
                "[OPTIONAL READER] symbol_flags DB not found → %s",
                SYMBOL_FLAGS_DB
            )

            return pd.DataFrame()

        conn = sqlite3.connect(
            SYMBOL_FLAGS_DB,
            check_same_thread=False
        )

        if not _table_exists(conn, "symbol_flags"):

            logger.warning(
                "[OPTIONAL READER] table symbol_flags not found"
            )

            conn.close()

            return pd.DataFrame()

        query = """
        SELECT
            symbol,
            symbolname
        FROM symbol_flags
        """

        df = pd.read_sql_query(query, conn)

        conn.close()

        df = _sanitize_dataframe(df)

        logger.info(
            "[OPTIONAL READER] loaded symbol_flags rows=%d",
            len(df)
        )

        return df

    except Exception:

        logger.exception("[OPTIONAL READER] symbol_flags load failed")

        return pd.DataFrame()


# ============================================================
# optional_master loader（FALLBACK）
# ============================================================

def _load_from_optional_master() -> pd.DataFrame:

    try:

        if not _db_exists(OPTIONAL_DB_PATH):

            logger.warning(
                "[OPTIONAL READER] optional DB not found → %s",
                OPTIONAL_DB_PATH
            )

            return pd.DataFrame()

        conn = sqlite3.connect(
            OPTIONAL_DB_PATH,
            check_same_thread=False
        )

        if not _table_exists(conn, "optional_master"):

            logger.warning(
                "[OPTIONAL READER] table optional_master not found"
            )

            conn.close()

            return pd.DataFrame()

        query = "SELECT * FROM optional_master"

        df = pd.read_sql_query(query, conn)

        conn.close()

        df = _sanitize_dataframe(df)

        logger.info(
            "[OPTIONAL READER] loaded optional_master rows=%d",
            len(df)
        )

        return df

    except Exception:

        logger.exception("[OPTIONAL READER] optional_master load failed")

        return pd.DataFrame()


# ============================================================
# Public API
# ============================================================

def load_optional_dataframe() -> pd.DataFrame:
    """
    optional DataFrame loader

    Priority
    --------
    1. symbol_flags.db
    2. optional_master
    """

    try:

        # --------------------------------------------------
        # ① symbol_flags（PRIMARY）
        # --------------------------------------------------

        df = _load_from_symbol_flags()

        if df is not None and not df.empty:
            return df

        # --------------------------------------------------
        # ② optional_master（FALLBACK）
        # --------------------------------------------------

        df = _load_from_optional_master()

        if df is not None and not df.empty:
            return df

        logger.warning(
            "[OPTIONAL READER] no optional data found"
        )

        return pd.DataFrame()

    except Exception:

        logger.exception("[OPTIONAL READER] load failed")

        return pd.DataFrame()