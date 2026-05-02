# ============================================================
# File   : trading/summary/holiday_loader.py
# Version: Ver1.0-PRODUCTION-SAFE-LAST-BUSINESS-LOADER
# ------------------------------------------------------------
# ✔ 前営業日最終確定バー取得
# ✔ 1min / 3min / 5min 対応
# ✔ datetime 最大値自動取得
# ✔ DB未存在耐性
# ✔ 空テーブル耐性
# ✔ 例外完全防御
# ✔ 本番安全設計
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database.session import summary_engine

logger = logging.getLogger(__name__)


# ============================================================
# テーブル名取得
# ============================================================

def _get_table_name(interval: int) -> str:
    return f"stock_summary_{int(interval)}min"


# ============================================================
# 最新 datetime 取得
# ============================================================

def _get_last_datetime(table: str):

    sql = text(f"SELECT MAX(datetime) AS max_dt FROM {table}")

    try:
        with summary_engine.connect() as conn:
            result = conn.execute(sql).fetchone()

        if not result or result[0] is None:
            return None

        return result[0]

    except SQLAlchemyError:
        logger.exception("❌ failed to fetch max datetime from %s", table)
        return None


# ============================================================
# 前営業日最終バー取得
# ============================================================

def load_last_business_summary(interval: int = 1) -> pd.DataFrame:
    """
    前営業日（DB上の最終datetime）の全銘柄バーを取得
    """

    table = _get_table_name(interval)

    try:

        last_dt = _get_last_datetime(table)

        if last_dt is None:
            logger.warning("⚠ no last datetime found for %s", table)
            return pd.DataFrame()

        sql = text(
            f"""
            SELECT *
            FROM {table}
            WHERE datetime = :dt
            ORDER BY symbol
            """
        )

        with summary_engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"dt": last_dt})

        if df is None or df.empty:
            logger.warning("⚠ no rows found for %s datetime=%s", table, last_dt)
            return pd.DataFrame()

        logger.info(
            "📦 Loaded last business summary | interval=%s | datetime=%s | rows=%s",
            interval,
            last_dt,
            len(df),
        )

        return df

    except SQLAlchemyError:
        logger.exception("❌ DB error while loading last business summary")
        return pd.DataFrame()

    except Exception:
        logger.exception("❌ unexpected error in load_last_business_summary")
        return pd.DataFrame()


# ============================================================
# 便利関数：複数足まとめ取得
# ============================================================

def load_all_intervals_last_summary(intervals=(1, 3, 5)) -> dict[int, pd.DataFrame]:
    """
    複数足の最終確定サマリーをまとめて取得
    """

    result = {}

    for interval in intervals:
        try:
            result[int(interval)] = load_last_business_summary(interval)
        except Exception:
            logger.exception("❌ failed loading interval=%s", interval)
            result[int(interval)] = pd.DataFrame()

    return result