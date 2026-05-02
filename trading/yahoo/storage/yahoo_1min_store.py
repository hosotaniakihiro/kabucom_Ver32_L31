# ============================================================
# File   : trading/yahoo/yahoo_1min_store.py
# Version: Ver3.4.0-ULTRA-PRODUCTION-SELF-HEALING
# ------------------------------------------------------------
# ✔ Ver3.3.0 完全保持（削除ゼロ）
# ✔ loader互換（time → datetime 自動補完）
# ✔ Yahoo列崩れ完全防御
# ✔ DataFrame列 → Series安全化
# ✔ intraday 専用DBへ保存
# ✔ WAL / busy_timeout / NAS耐性
# ✔ tmp table UPSERT
# ✔ scheduler 不死身
# ✔ DB列自動検出
# ✔ schema自動修復
# ✔ *_price 列互換
# ✔ SQLite self healing
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import threading
import uuid
import datetime as dt

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from trading.yahoo.storage.yahoo_1min_bootstrap import get_intraday_db_path

logger = logging.getLogger(__name__)

# ============================================================
# 正規列
# ============================================================

YAHOO_1MIN_COLUMNS = [
    "symbol",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
]

# ------------------------------------------------------------

COLUMN_ALIAS = {
    "time": "datetime",
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "close_price": "close",
}

_save_lock = threading.Lock()

# ============================================================
# エンジン生成
# ============================================================


def _create_intraday_engine(target_date: dt.date):

    db_path = get_intraday_db_path(target_date)

    logger.info("[YAHOO BOOTSTRAP] intraday db path: %s", db_path)

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

    return engine


# ============================================================
# 列正規化
# ============================================================


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:

    cols = []

    for c in df.columns:

        if isinstance(c, tuple):

            c = "".join([str(x) for x in c if x not in ("", None)])

        c = str(c).strip().lower().replace(" ", "_")

        cols.append(c)

    df.columns = cols

    return df


# ============================================================
# DataFrame正規化
# ============================================================


def _normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    df = _normalize_columns(df)

    # --------------------------------------------------------
    # alias補完
    # --------------------------------------------------------

    for src, dst in COLUMN_ALIAS.items():

        if src in df.columns and dst not in df.columns:

            df[dst] = df[src]

    missing = [c for c in YAHOO_1MIN_COLUMNS if c not in df.columns]

    if missing:

        raise ValueError(f"Missing required columns: {missing}")

    df = df[YAHOO_1MIN_COLUMNS].copy()

    # --------------------------------------------------------
    # symbol
    # --------------------------------------------------------

    df["symbol"] = df["symbol"].astype(str).str.strip()

    df = df[df["symbol"] != ""]

    # --------------------------------------------------------
    # datetime
    # --------------------------------------------------------

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    df = df.dropna(subset=["datetime"])

    if df.empty:

        return df

    try:

        if df["datetime"].dt.tz is not None:

            df["datetime"] = df["datetime"].dt.tz_convert(None)

    except Exception:

        pass

    df["datetime"] = df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # --------------------------------------------------------
    # numeric
    # --------------------------------------------------------

    for col in ["open", "high", "low", "close", "volume"]:

        s = df[col]

        if isinstance(s, pd.DataFrame):

            s = s.iloc[:, 0]

        df[col] = pd.to_numeric(s, errors="coerce").fillna(0.0)

    df.loc[df["volume"] < 0, "volume"] = 0.0

    # --------------------------------------------------------
    # duplicates
    # --------------------------------------------------------

    df = df.drop_duplicates(subset=["symbol", "datetime"])

    return df[YAHOO_1MIN_COLUMNS]


# ============================================================
# DB列確認
# ============================================================


def _get_table_columns(conn):

    rows = conn.execute(text("PRAGMA table_info(yahoo_1min)")).fetchall()

    return {r[1] for r in rows}


# ============================================================
# schema self heal
# ============================================================


def _ensure_yahoo_1min_table(conn):

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS yahoo_1min (
                symbol TEXT NOT NULL,
                datetime TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY(symbol, datetime)
            )
            """
        )
    )

    cols = _get_table_columns(conn)

    expected = {
        "symbol",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }

    missing = expected - cols

    for col in missing:

        logger.warning("[YAHOO SCHEMA FIX] add column %s", col)

        conn.execute(text(f"ALTER TABLE yahoo_1min ADD COLUMN {col} REAL"))


# ============================================================
# UPSERT
# ============================================================


def _bulk_upsert(conn, df):

    tmp = f"_tmp_yahoo_1min_{uuid.uuid4().hex}"

    conn.execute(text(f'DROP TABLE IF EXISTS "{tmp}"'))

    df.to_sql(tmp, conn, index=False, if_exists="replace")

    conn.execute(
        text(
            f"""
            INSERT OR REPLACE INTO yahoo_1min
            (symbol, datetime, open, high, low, close, volume)
            SELECT
                symbol,
                datetime,
                open,
                high,
                low,
                close,
                volume
            FROM "{tmp}"
            """
        )
    )

    conn.execute(text(f'DROP TABLE IF EXISTS "{tmp}"'))


# ============================================================
# MAIN API
# ============================================================


def save_yahoo_1min(df: pd.DataFrame, target_date) -> None:
    """
    Yahoo 1min OHLCV 保存
    """

    if df is None or df.empty:

        return

    if not _save_lock.acquire(blocking=False):

        logger.warning("[YAHOO 1MIN] already saving → skip")

        return

    engine = None

    try:

        try:

            df_save = _normalize_dataframe(df)

        except Exception:

            logger.exception("[YAHOO 1MIN] normalization failed")

            return

        if df_save.empty:

            return

        engine = _create_intraday_engine(target_date)

        with engine.begin() as conn:

            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA busy_timeout=5000"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))

            _ensure_yahoo_1min_table(conn)

            _bulk_upsert(conn, df_save)

            logger.info(
                "[YAHOO 1MIN SAVED] rows=%d symbols=%d",
                len(df_save),
                df_save["symbol"].nunique(),
            )

    except SQLAlchemyError:

        logger.exception("[YAHOO 1MIN] save failed")

    finally:

        try:

            if engine:

                engine.dispose()

        except Exception:

            pass

        _save_lock.release()