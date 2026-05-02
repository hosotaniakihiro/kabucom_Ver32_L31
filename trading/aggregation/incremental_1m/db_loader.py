"""
============================================================
db_loader.py
Incremental1MEngine PUSH DB Loader
------------------------------------------------------------
✔ PUSH DBからtick取得
✔ 差分ロード
✔ DuckDB / SQLite両対応
✔ stream_data存在チェック
✔ カラム自動検出
✔ datetime正規化
✔ pandas DataFrame返却
✔ 本番安定版
============================================================
"""

from __future__ import annotations

import pandas as pd
import logging

from sqlalchemy import text

from database.session import get_push_engine
from core.state.last_state_manager import last_state

from .utils import safe_dt

logger = logging.getLogger(__name__)


# ============================================================
# TABLE EXISTS
# ============================================================

def table_exists(conn, table_name: str):

    try:

        # DuckDB / PostgreSQL style

        try:

            result = conn.execute(
                text("""
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_name = :name
                """),
                {"name": table_name}
            ).fetchone()

            if result and result[0] > 0:
                return True

        except Exception:
            pass

        # SQLite fallback

        try:

            result = conn.execute(
                text("""
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table'
                    AND name=:name
                """),
                {"name": table_name}
            ).fetchone()

            if result:
                return True

        except Exception:
            pass

        return False

    except Exception:

        logger.exception(
            "[DB LOADER] table_exists failed"
        )

        return False


# ============================================================
# COLUMN DETECT
# ============================================================

def detect_columns(conn):

    try:

        # DuckDB

        try:

            cols = conn.execute(
                text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'stream_data'
                """)
            ).fetchall()

            return {c[0] for c in cols}

        except Exception:
            pass

        # SQLite fallback

        cols = conn.execute(
            text("PRAGMA table_info(stream_data)")
        ).fetchall()

        return {c[1] for c in cols}

    except Exception:

        logger.exception(
            "[DB LOADER] column detect failed"
        )

        return set()


# ============================================================
# LOAD PUSH DATA
# ============================================================

def load_new_push():

    last_push = last_state.get_last_push()

    try:

        with get_push_engine().connect() as conn:

            # ------------------------------------------------
            # table check
            # ------------------------------------------------

            if not table_exists(conn, "stream_data"):
                return pd.DataFrame()

            # ------------------------------------------------
            # column detect
            # ------------------------------------------------

            colnames = detect_columns(conn)

    except Exception:

        logger.exception(
            "[DB LOADER] stream_data existence check failed"
        )

        return pd.DataFrame()

    # --------------------------------------------------------
    # required columns
    # --------------------------------------------------------

    required = {"symbol", "datetime"}

    if not required.issubset(colnames):

        logger.error(
            "[DB LOADER] required columns missing"
        )

        return pd.DataFrame()

    # --------------------------------------------------------
    # select columns
    # --------------------------------------------------------

    select_cols = ["symbol", "datetime"]

    if "date" in colnames:
        select_cols.append("date")

    if "time" in colnames:
        select_cols.append("time")

    if "close_price" in colnames:
        select_cols.append("close_price")

    if "price" in colnames:
        select_cols.append("price")

    if "volume" in colnames:
        select_cols.append("volume")

    # --------------------------------------------------------
    # query
    # --------------------------------------------------------

    query = f"""
        SELECT {', '.join(select_cols)}
        FROM stream_data
    """

    params = {}

    if last_push:

        safe_last = safe_dt(last_push)

        if safe_last:

            query += " WHERE datetime > :last_dt"

            params["last_dt"] = safe_last

    query += " ORDER BY datetime ASC"

    # --------------------------------------------------------
    # execute
    # --------------------------------------------------------

    try:

        with get_push_engine().connect() as conn:

            df = pd.read_sql(
                text(query),
                conn,
                params=params
            )

        if df is None or df.empty:
            return pd.DataFrame()

        # ----------------------------------------------------
        # datetime normalize
        # ----------------------------------------------------

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        df = df.dropna(
            subset=["datetime"]
        )

        # ----------------------------------------------------
        # date / time fallback
        # ----------------------------------------------------

        if "date" not in df.columns:
            df["date"] = df["datetime"].dt.date

        if "time" not in df.columns:
            df["time"] = df["datetime"].dt.time

        return df

    except Exception:

        logger.exception(
            "[DB LOADER] PUSH load failed"
        )

        return pd.DataFrame()