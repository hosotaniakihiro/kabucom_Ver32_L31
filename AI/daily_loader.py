import sqlite3
import pandas as pd
import os
import datetime as dt


# ================================
# ★ 日足DBパス（環境に合わせて変更）
# ================================
DAILY_DB_PATH = "y:/stock_price_data/daily_price.db"

# 何ヶ月分取得するか
USE_MONTHS = 12


# ================================
# ★ 日足読込（最新 MTF-AI 対応）
# ================================
def load_daily_last_12m():

    # DBの存在チェック
    if not os.path.exists(DAILY_DB_PATH):
        raise FileNotFoundError(f"❌ 日足DBが見つかりません → {DAILY_DB_PATH}")

    # 必要カラムのみロード（高速）
    query = """
        SELECT
            symbol,
            date,
            open,
            high,
            low,
            close,
            volume
        FROM stock_daily
    """

    conn = sqlite3.connect(DAILY_DB_PATH)
    df = pd.read_sql(query, conn)
    conn.close()

    # symbol を Int64 へ（MTF結合で必須）
    df["symbol"] = pd.to_numeric(df["symbol"], errors="coerce").astype("Int64")

    # date を datetime 化
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    # 直近12ヶ月だけ抽出
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=30 * USE_MONTHS)
    df = df[df["date"].dt.date >= cutoff]

    # 欠損除外
    df = df.dropna(subset=["symbol", "open", "high", "low", "close"])

    # 並べ替え
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

    return df
