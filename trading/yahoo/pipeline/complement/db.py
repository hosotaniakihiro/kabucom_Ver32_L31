# ============================================================
# File   : trading/yahoo/pipeline/complement/db.py
# Version: PRODUCTION-STABLE-REV4.1-YAHOO-COMPLEMENT-DB
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完パイプライン用 summary DB ユーティリティ
#
# 【主な機能】
#   - summaryYYYYMMDD.db のパス解決
#   - SQLite 接続生成
#   - table 存在確認
#   - Yahoo補完済み source の最新 datetime 取得
#
# 【重要】
#   - 差分判定では PUSH由来 source の最新時刻は見ない
#   - 必ず Yahoo補完 source:
#       summary_recovery_yahoo_1m
#       summary_recovery_yahoo_resample_3m
#       summary_recovery_yahoo_resample_5m
#     の MAX(datetime) を見る
#
# 【用途】
#   - runner.py から latest_yahoo_dt を取得する
#   - diff.py の保存差分判定に使う
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from .constants import (
    DEFAULT_BASE_DIR,
    summary_table_for_interval,
    yahoo_source_for_interval,
    push_source_for_interval,
)

logger = logging.getLogger(__name__)


# ============================================================
# date / path helpers
# ============================================================

def today_yyyymmdd(now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now()
    return now.strftime("%Y%m%d")


def today_date_str(now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now()
    return now.strftime("%Y-%m-%d")


def resolve_base_dir(base_dir: Optional[str] = None) -> str:
    if base_dir:
        return str(base_dir)

    try:
        env = os.environ.get("AUTOSTOCK_BASE_DIR")
        if env:
            return str(env)
    except Exception:
        pass

    return DEFAULT_BASE_DIR


def get_summary_db_path(
    *,
    date_yyyymmdd: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> str:
    """
    summaryYYYYMMDD.db のフルパスを返す。
    """
    d = date_yyyymmdd or today_yyyymmdd()
    root = resolve_base_dir(base_dir)

    return str(
        Path(root)
        / "raw_data"
        / "kabu_station"
        / "summary"
        / f"summary{d}.db"
    )


# ============================================================
# sqlite helpers
# ============================================================

def connect_sqlite(path: str, timeout: float = 10.0) -> sqlite3.Connection:
    """
    SQLite接続を生成する。

    Yahoo補完は低優先なので、ここでは接続の安全設定のみ行う。
    実際の保存ロック制御は save.py / summary_saver_bulk 側に委譲する。
    """
    con = sqlite3.connect(path, timeout=timeout)

    try:
        con.execute("PRAGMA busy_timeout = 10000")
    except Exception:
        pass

    try:
        con.execute("PRAGMA journal_mode = WAL")
    except Exception:
        pass

    try:
        con.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass

    return con


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        return cur.fetchone() is not None
    except Exception:
        logger.debug("[YAHOO DB] table_exists failed table=%s", table, exc_info=True)
        return False


def get_table_columns(
    table: str,
    *,
    summary_db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    date_yyyymmdd: Optional[str] = None,
) -> list[str]:
    """
    指定summaryテーブルのカラム一覧を返す。
    """
    db_path = summary_db_path or get_summary_db_path(
        date_yyyymmdd=date_yyyymmdd,
        base_dir=base_dir,
    )

    if not os.path.exists(db_path):
        logger.info("[YAHOO DB] db not found for columns path=%s", db_path)
        return []

    try:
        with connect_sqlite(db_path) as con:
            if not table_exists(con, table):
                logger.info("[YAHOO DB] table not found for columns table=%s path=%s", table, db_path)
                return []

            cur = con.execute(f"PRAGMA table_info({table})")
            rows = cur.fetchall()
            return [str(r[1]) for r in rows]

    except Exception:
        logger.exception("[YAHOO DB] get_table_columns failed table=%s path=%s", table, db_path)
        return []


# ============================================================
# datetime helpers
# ============================================================

def _parse_timestamp(value) -> Optional[pd.Timestamp]:
    try:
        if value is None:
            return None

        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None

        ts = pd.Timestamp(ts)

        try:
            ts = ts.tz_localize(None)
        except Exception:
            try:
                ts = ts.tz_convert(None)
            except Exception:
                pass

        return ts

    except Exception:
        return None


# ============================================================
# latest datetime queries
# ============================================================

def get_latest_summary_datetime_by_source(
    interval: int,
    *,
    source: str,
    summary_db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    date_yyyymmdd: Optional[str] = None,
) -> Optional[pd.Timestamp]:
    """
    指定 source の MAX(datetime) を取得する。
    """
    table = summary_table_for_interval(interval)

    db_path = summary_db_path or get_summary_db_path(
        date_yyyymmdd=date_yyyymmdd,
        base_dir=base_dir,
    )

    if not os.path.exists(db_path):
        logger.info(
            "[YAHOO DB] summary db not found latest_dt interval=%s source=%s path=%s",
            interval,
            source,
            db_path,
        )
        return None

    try:
        with connect_sqlite(db_path) as con:
            if not table_exists(con, table):
                logger.info(
                    "[YAHOO DB] table not found latest_dt table=%s interval=%s source=%s path=%s",
                    table,
                    interval,
                    source,
                    db_path,
                )
                return None

            cur = con.execute(
                f"SELECT MAX(datetime) FROM {table} WHERE source = ?",
                (source,),
            )
            row = cur.fetchone()
            value = row[0] if row else None

        ts = _parse_timestamp(value)

        logger.info(
            "[YAHOO DB] latest summary dt interval=%s table=%s source=%s dt=%s",
            interval,
            table,
            source,
            ts,
        )

        return ts

    except Exception:
        logger.exception(
            "[YAHOO DB] get latest summary datetime failed interval=%s table=%s source=%s path=%s",
            interval,
            table,
            source,
            db_path,
        )
        return None


def get_latest_yahoo_summary_datetime(
    interval: int,
    *,
    summary_db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    date_yyyymmdd: Optional[str] = None,
) -> Optional[pd.Timestamp]:
    """
    Yahoo補完済み source の最新 datetime を取得する。

    重要:
      - PUSH由来 source は見ない
      - 差分補完の基準は Yahoo source のみ
    """
    source = yahoo_source_for_interval(interval)

    return get_latest_summary_datetime_by_source(
        interval,
        source=source,
        summary_db_path=summary_db_path,
        base_dir=base_dir,
        date_yyyymmdd=date_yyyymmdd,
    )


def get_latest_push_summary_datetime(
    interval: int,
    *,
    summary_db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    date_yyyymmdd: Optional[str] = None,
) -> Optional[pd.Timestamp]:
    """
    PUSH由来 source の最新 datetime を取得する。
    通常のYahoo差分判定には使わない。
    診断ログ・比較用。
    """
    source = push_source_for_interval(interval)

    return get_latest_summary_datetime_by_source(
        interval,
        source=source,
        summary_db_path=summary_db_path,
        base_dir=base_dir,
        date_yyyymmdd=date_yyyymmdd,
    )


def get_latest_any_summary_datetime(
    interval: int,
    *,
    summary_db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    date_yyyymmdd: Optional[str] = None,
) -> Optional[pd.Timestamp]:
    """
    source を問わず MAX(datetime) を取得する。
    診断用。
    Yahoo差分判定には使わない。
    """
    table = summary_table_for_interval(interval)

    db_path = summary_db_path or get_summary_db_path(
        date_yyyymmdd=date_yyyymmdd,
        base_dir=base_dir,
    )

    if not os.path.exists(db_path):
        logger.info(
            "[YAHOO DB] summary db not found latest_any interval=%s path=%s",
            interval,
            db_path,
        )
        return None

    try:
        with connect_sqlite(db_path) as con:
            if not table_exists(con, table):
                logger.info(
                    "[YAHOO DB] table not found latest_any table=%s interval=%s path=%s",
                    table,
                    interval,
                    db_path,
                )
                return None

            cur = con.execute(f"SELECT MAX(datetime) FROM {table}")
            row = cur.fetchone()
            value = row[0] if row else None

        ts = _parse_timestamp(value)

        logger.info(
            "[YAHOO DB] latest any summary dt interval=%s table=%s dt=%s",
            interval,
            table,
            ts,
        )

        return ts

    except Exception:
        logger.exception(
            "[YAHOO DB] get latest any summary datetime failed interval=%s table=%s path=%s",
            interval,
            table,
            db_path,
        )
        return None


def get_latest_datetimes_report(
    interval: int,
    *,
    summary_db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    date_yyyymmdd: Optional[str] = None,
) -> dict[str, Optional[pd.Timestamp]]:
    """
    診断用。
    Yahoo / PUSH / ANY の最新時刻をまとめて返す。
    """
    yahoo_dt = get_latest_yahoo_summary_datetime(
        interval,
        summary_db_path=summary_db_path,
        base_dir=base_dir,
        date_yyyymmdd=date_yyyymmdd,
    )
    push_dt = get_latest_push_summary_datetime(
        interval,
        summary_db_path=summary_db_path,
        base_dir=base_dir,
        date_yyyymmdd=date_yyyymmdd,
    )
    any_dt = get_latest_any_summary_datetime(
        interval,
        summary_db_path=summary_db_path,
        base_dir=base_dir,
        date_yyyymmdd=date_yyyymmdd,
    )

    logger.info(
        "[YAHOO DB] latest report interval=%s yahoo=%s push=%s any=%s",
        interval,
        yahoo_dt,
        push_dt,
        any_dt,
    )

    return {
        "yahoo": yahoo_dt,
        "push": push_dt,
        "any": any_dt,
    }


# ============================================================
# public
# ============================================================

__all__ = [
    "today_yyyymmdd",
    "today_date_str",
    "resolve_base_dir",
    "get_summary_db_path",
    "connect_sqlite",
    "table_exists",
    "get_table_columns",
    "get_latest_summary_datetime_by_source",
    "get_latest_yahoo_summary_datetime",
    "get_latest_push_summary_datetime",
    "get_latest_any_summary_datetime",
    "get_latest_datetimes_report",
]