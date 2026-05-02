# ============================================================
# trading/summary/db_reader.py
# Ver3.0-PRODUCTION-DAILY-DB-SAFE-FINAL
# ------------------------------------------------------------
# ✔ 日付別 summaryDB 対応
# ✔ 前日3m/5mロード（MA75維持）
# ✔ 今日最後の1mロード
# ✔ datetime完全正規化
# ✔ NaN / inf 完全排除
# ✔ global_data 反映
# ✔ 例外完全吸収
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np
from datetime import date, timedelta
from pathlib import Path

from database.session import Session_summary
from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)
from config.paths import get_path
from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# 共通ユーティリティ
# ============================================================

def _safe_clean(df: pd.DataFrame) -> pd.DataFrame:
    return df.replace([np.inf, -np.inf], np.nan)


def _to_df(rows) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([r.__dict__ for r in rows])
    df = df.drop(columns=["_sa_instance_state"], errors="ignore")

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    df = df.dropna(subset=["datetime"], errors="ignore")
    df = _safe_clean(df)

    return df


def _get_summary_db_path(target_date: date) -> Path:
    base_dir = get_path("summary_db_dir")
    return base_dir / f"summary{target_date.strftime('%Y%m%d')}.db"


# ============================================================
# 前日3m / 5mロード（MA75維持用）
# ============================================================

def load_previous_tf(interval: int):

    if interval not in (3, 5):
        return

    try:

        prev_date = date.today() - timedelta(days=1)
        db_path = _get_summary_db_path(prev_date)

        if not db_path.exists():
            logger.warning(f"[DB_READER] prev DB not found: {db_path}")
            return

        session = Session_summary(db_path=db_path)

        model = StockSummary3Min if interval == 3 else StockSummary5Min

        rows = (
            session.query(model)
            .order_by(model.datetime.desc())
            .limit(1000)
            .all()
        )

        session.close()

        df = _to_df(rows)

        if df.empty:
            logger.warning(f"[DB_READER] prev {interval}min empty")
            return

        df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)

        global_data.set_merged_summary(interval, df)

        logger.info(
            f"📦 loaded previous {interval}min rows={len(df)}"
        )

    except Exception:
        logger.exception(f"[DB_READER] load_previous_tf({interval}) failed")


# ============================================================
# 今日最後の1分足ロード
# ============================================================

def load_today_last_1m():

    try:

        today = date.today()
        db_path = _get_summary_db_path(today)

        if not db_path.exists():
            logger.warning(f"[DB_READER] today DB not found: {db_path}")
            return

        session = Session_summary(db_path=db_path)

        rows = (
            session.query(StockSummary1Min)
            .order_by(StockSummary1Min.datetime.desc())
            .limit(1000)
            .all()
        )

        session.close()

        df = _to_df(rows)

        if df.empty:
            logger.warning("[DB_READER] today 1min empty")
            return

        df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)

        global_data.set_merged_summary(1, df)

        logger.info(
            f"📦 loaded today last 1min rows={len(df)}"
        )

    except Exception:
        logger.exception("[DB_READER] load_today_last_1m failed")