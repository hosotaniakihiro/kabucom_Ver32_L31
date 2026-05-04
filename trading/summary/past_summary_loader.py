# ============================================================
# trading/summary/past_summary_loader.py（Ver17.0）
# ------------------------------------------------------------
# 前営業日・前々営業日の summary DB からサマリーをロードする
# - DB名は summaryYYYYMMDD.db
# - 1/3/5 分足すべてロード可能
# - 欠損時は空 DataFrame を返す（安全）
# ============================================================

import os
import pandas as pd
import sqlite3
import logging
import datetime as dt

from database.session import BASE_PATH
from utils.business_day_utils import get_recent_business_days

logger = logging.getLogger(__name__)


# ============================================================
# 指定日の summary DB を読み込む
# ============================================================
def _load_summary_db(target_date: dt.date):
    """
    summaryYYYYMMDD.db を DataFrame 辞書で返す
    例: {"1min": df1, "3min": df3, "5min": df5}
    """
    date_str = target_date.strftime("%Y%m%d")
    db_path = os.path.join(BASE_PATH, f"summary{date_str}.db")

    if not os.path.exists(db_path):
        logger.warning(f"⚠ 過去DBなし: {db_path}")
        return {"1min": pd.DataFrame(), "3min": pd.DataFrame(), "5min": pd.DataFrame()}

    try:
        with sqlite3.connect(db_path) as conn:
            df1 = pd.read_sql("SELECT * FROM stock_summary_1min", conn)
            df3 = pd.read_sql("SELECT * FROM stock_summary_3min", conn)
            df5 = pd.read_sql("SELECT * FROM stock_summary_5min", conn)

        # 日付・時刻を補正
        for df in (df1, df3, df5):
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
                df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.time

        return {"1min": df1, "3min": df3, "5min": df5}

    except Exception as e:
        logger.error(f"❌ DB読み込みエラー: {db_path}: {e}", exc_info=True)
        return {"1min": pd.DataFrame(), "3min": pd.DataFrame(), "5min": pd.DataFrame()}


# ============================================================
# 🔥 過去2営業日の 1/3/5分サマリーをまとめて返す
# ============================================================
def load_past_summaries(days: int = 2):
    """
    前営業日・前々営業日からサマリーをロードする
    days=2 → (T-1, T-2)
    """
    try:
        target_days = get_recent_business_days(days)
        results = []

        for d in target_days:
            loaded = _load_summary_db(d)
            results.append((d, loaded))

        return results

    except Exception as e:
        logger.error(f"❌ 過去サマリー読み込み失敗: {e}", exc_info=True)
        return []


# ============================================================
# 🔹 最新1日 + 過去2日 を連結して MA 計算用 DF を作る
# ============================================================
def concat_history(df_today: pd.DataFrame, df_list: list):
    """
    df_today: 今日のサマリー DataFrame（1/3/5 どれでもOK）
    df_list: load_past_summaries() の戻り値
    """

    frames = [df_today] if df_today is not None else []

    for _, past in df_list:
        # past is {"1min": df, "3min": df, "5min": df}
        # ここでは同じ分足のものだけ使う
        if df_today is None or df_today.empty:
            continue

        tf_name = None
        if "stock_summary_1min" in df_today.columns:
            tf_name = "1min"
        frames.append(past.get(tf_name, pd.DataFrame()))

    if not frames:
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)

    # 日付昇順・時間昇順に揃える
    if "date" in df_all.columns and "time" in df_all.columns:
        df_all = df_all.sort_values(["date", "time"]).reset_index(drop=True)

    return df_all
