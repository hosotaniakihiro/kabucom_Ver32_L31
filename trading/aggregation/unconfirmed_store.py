# ============================================================
# unconfirmed_store.py
# Ver3.3-SQLITE-RUNTIME-STABLE-FINAL
# ------------------------------------------------------------
# ✔ 未確定1分足DB保存（SQLite側）
# ✔ 再起動完全復元
# ✔ UPSERT対応
# ✔ 確定時削除
# ✔ Timestamp完全対応
# ✔ tz-naive統一
# ✔ 例外安全ログ
# ✔ トランザクション破壊防止（ROLLBACK強制）
# ✔ 本番超安定版
# ✔ SQLAlchemy 2.0 完全対応
# ✔ named bind 使用（安全）
# ✔ DuckDB非依存（ロック衝突完全排除）
# ============================================================

from __future__ import annotations

import pandas as pd
import datetime as dt
import logging
from sqlalchemy import text

from database.session import get_push_engine

logger = logging.getLogger(__name__)

TABLE = "runtime_unconfirmed_1m"


# ============================================================
# 安全変換
# ============================================================

def _to_sql_value(v):

    if v is None:
        return None

    if isinstance(v, pd.Timestamp):
        if v.tzinfo:
            v = v.tz_convert("Asia/Tokyo").tz_localize(None)
        return v.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(v, dt.datetime):
        if v.tzinfo:
            v = v.astimezone(
                dt.timezone(dt.timedelta(hours=9))
            ).replace(tzinfo=None)
        return v.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d")

    if isinstance(v, (int, float)):
        return float(v)

    return str(v)


# ============================================================
# テーブル保証（SQLite側）
# ============================================================

def ensure_table():

    conn = None

    try:
        conn = get_push_engine().connect()

        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    symbol TEXT NOT NULL,
                    minute TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    PRIMARY KEY(symbol, minute)
                )
            """)
        )

    except Exception:
        logger.exception("[unconfirmed] ensure_table failed")

        if conn:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:
                pass

    finally:
        if conn:
            conn.close()


# ============================================================
# 全件ロード（再起動復元）
# ============================================================

def load_all() -> dict:

    ensure_table()

    conn = None

    try:
        conn = get_push_engine().connect()

        df = pd.read_sql(
            text(f"SELECT * FROM {TABLE}"),
            conn
        )

        if df.empty:
            return {}

        df["minute"] = pd.to_datetime(
            df["minute"],
            errors="coerce"
        )

        df = df.dropna(subset=["minute"])

        cache = {}

        for _, r in df.iterrows():
            cache[str(r["symbol"])] = {
                "minute": r["minute"],
                "open": float(r["open"]) if r["open"] is not None else 0.0,
                "high": float(r["high"]) if r["high"] is not None else 0.0,
                "low": float(r["low"]) if r["low"] is not None else 0.0,
                "close": float(r["close"]) if r["close"] is not None else 0.0,
                "volume": float(r["volume"]) if r["volume"] is not None else 0.0,
            }

        return cache

    except Exception:
        logger.exception("[unconfirmed] load_all failed")

        if conn:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:
                pass

        return {}

    finally:
        if conn:
            conn.close()


# ============================================================
# UPSERT（SQLite安全版）
# ============================================================

def upsert(symbol: str, bar: dict):

    ensure_table()

    conn = None

    try:
        minute = _to_sql_value(bar.get("minute"))

        sql = text(f"""
            INSERT INTO {TABLE}
            (symbol, minute, open, high, low, close, volume)
            VALUES (:symbol, :minute, :open, :high, :low, :close, :volume)
            ON CONFLICT(symbol, minute) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume
        """)

        params = {
            "symbol": str(symbol),
            "minute": minute,
            "open": float(bar.get("open", 0) or 0),
            "high": float(bar.get("high", 0) or 0),
            "low": float(bar.get("low", 0) or 0),
            "close": float(bar.get("close", 0) or 0),
            "volume": float(bar.get("volume", 0) or 0),
        }

        conn = get_push_engine().connect()
        conn.execute(sql, params)

    except Exception:
        logger.exception("[unconfirmed] upsert failed")

        if conn:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:
                pass

    finally:
        if conn:
            conn.close()


# ============================================================
# DELETE（確定後削除）
# ============================================================

def delete(symbol: str, minute):

    ensure_table()

    conn = None

    try:
        minute = _to_sql_value(minute)

        sql = text(f"""
            DELETE FROM {TABLE}
            WHERE symbol = :symbol
              AND minute = :minute
        """)

        conn = get_push_engine().connect()
        conn.execute(
            sql,
            {
                "symbol": str(symbol),
                "minute": minute,
            },
        )

    except Exception:
        logger.exception("[unconfirmed] delete failed")

        if conn:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:
                pass

    finally:
        if conn:
            conn.close()