# trading/ranking/update_ranking.py
import pandas as pd
import sqlite3
import datetime
import logging

logger = logging.getLogger(__name__)

def fetch_and_store_ranking(df: pd.DataFrame, db_path: str, table_name: str = "ranking"):
    """
    ランキングDataFrameをSQLiteに保存する
    """
    if df is None or df.empty:
        logger.warning("⚠️ ランキングDFが空 → 保存スキップ")
        return

    conn = sqlite3.connect(db_path)
    try:
        df["created_at"] = datetime.datetime.now()

        # ✅ 日ごとに使い捨て → テーブルを作り直して保存
        df.to_sql(table_name, conn, if_exists="replace", index=False)

        logger.info(f"✅ ランキング保存成功: {len(df)}件 → {db_path} ({table_name})")
    except Exception as e:
        logger.error(f"❌ ランキング保存失敗: {e}")
    finally:
        conn.close()
