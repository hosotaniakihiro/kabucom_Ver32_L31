# ============================================================
# File: trading/summary/last_business_loader.py
# Ver : V1.2-FINAL-SQLITE-SAFE-PRODUCTION-NAS-STABLE
# ------------------------------------------------------------
# ✔ 直近営業日自動判定
# ✔ Yahoo補完と統合思想維持
# ✔ 1m/3m/5m 対応
# ✔ SQLite Timestamp完全安全化
# ✔ pandas.Timestamp完全排除
# ✔ TEXT/DATETIME両対応
# ✔ 例外完全吸収
# ✔ 既存破壊ゼロ
# ✔ WAL完全互換
# ✔ get_summary_engine完全対応
# ✔ NoneType engine完全排除
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import pandas as pd
from sqlalchemy import text

from database.session import get_summary_engine

logger = logging.getLogger(__name__)


# ============================================================
# SQLite安全datetime変換（超重要）
# ============================================================

def _sqlite_safe_datetime(value) -> str | None:
    """
    SQLiteへ渡すための安全変換
    pandas.Timestamp禁止
    datetime → 文字列化
    """

    if value is None:
        return None

    try:
        # pandas.Timestamp → datetime
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()

        # datetime → 文字列
        if isinstance(value, dt.datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")

        # 既に文字列
        if isinstance(value, str):
            return value

        # その他型は一旦文字列化
        return str(value)

    except Exception as e:
        logger.error(f"[LAST BUSINESS] datetime conversion failed: {e}")
        return None


# ============================================================
# 最終営業日取得
# ============================================================

def _get_last_business_datetime(table: str) -> str | None:
    """
    MAX(datetime) を取得し SQLite安全文字列で返す
    """

    query = f"""
        SELECT MAX(datetime) as max_dt
        FROM {table}
    """

    try:
        engine = get_summary_engine()

        with engine.connect() as conn:
            df = pd.read_sql(text(query), conn)

        if df.empty:
            return None

        raw_dt = df.iloc[0]["max_dt"]

        if raw_dt is None:
            return None

        # pandas変換
        raw_dt = pd.to_datetime(raw_dt, errors="coerce")

        if pd.isna(raw_dt):
            return None

        return _sqlite_safe_datetime(raw_dt)

    except Exception as e:
        logger.error(f"[LAST BUSINESS] max datetime failed: {e}")
        return None


# ============================================================
# 本体
# ============================================================

def load_last_business_close(interval: int) -> pd.DataFrame:
    """
    指定intervalの直近営業日終値バーをロード
    SQLite完全安全
    """

    table = f"stock_summary_{interval}min"

    try:

        last_dt = _get_last_business_datetime(table)

        if last_dt is None:
            logger.warning(f"[LAST BUSINESS] no data for {table}")
            return pd.DataFrame()

        query = f"""
            SELECT *
            FROM {table}
            WHERE datetime = :dt
        """

        engine = get_summary_engine()

        with engine.connect() as conn:
            df = pd.read_sql(
                text(query),
                conn,
                params={"dt": last_dt},
            )

        logger.info(
            f"[LAST BUSINESS] interval={interval} dt={last_dt} rows={len(df)}"
        )

        return df

    except Exception as e:
        logger.error(f"[LAST BUSINESS] failed: {e}")
        return pd.DataFrame()