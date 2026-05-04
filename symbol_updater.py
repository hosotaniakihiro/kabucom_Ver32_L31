# symbol_updater.py
import os
import pandas as pd
import sqlite3
import datetime as dt
import schedule
import time
import threading

# === 設定 ===
PKL_FILE = "y:/kabu/symbols.pkl"
BASE_PATH = "y:/stock_price_data/"
TARGET_COUNT = 100  # 常に100銘柄固定


def get_summary_db_path():
    """本日の日付に基づいた summaryDB パスを返す"""
    today = dt.datetime.now().strftime("%Y%m%d")
    return os.path.join(BASE_PATH, f"summary{today}.db")


def get_top_symbols_by_volume_volatility():
    """出来高×変動率の上位100銘柄を返す"""
    db_path = get_summary_db_path()
    if not os.path.exists(db_path):
        print(f"⚠️ DBが存在しません: {db_path}")
        return pd.DataFrame()

    conn = sqlite3.connect(db_path)
    today = dt.date.today().strftime("%Y-%m-%d")

    query = f"""
        SELECT symbol, symbolname, close_price, volume, time_range
        FROM stock_summary
        WHERE date = '{today}'
    """
    df = pd.read_sql(query, conn)
    conn.close()

    if df.empty:
        print("⚠️ DBに今日のデータがありません")
        return pd.DataFrame()

    # 変動率（直近と1つ前の終値差分％）
    df = df.sort_values(["symbol", "time_range"])
    df["pct_change"] = df.groupby("symbol")["close_price"].pct_change() * 100
    df = df.dropna()

    # 出来高 × 変動率
    df["score"] = abs(df["pct_change"]) * df["volume"]

    # 上位100銘柄
    top_df = (
        df.sort_values("score", ascending=False)
        .drop_duplicates("symbol")
        .head(TARGET_COUNT)[["symbol", "symbolname"]]
    )

    return top_df


def update_symbols():
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] ♻️ 出来高×変動率ランキングで監視銘柄を更新...")

    top_df = get_top_symbols_by_volume_volatility()
    if top_df.empty:
        print("⚠️ 更新対象が見つかりません")
        return

    # 常に100銘柄に調整
    if len(top_df) < TARGET_COUNT:
        print(f"⚠️ 銘柄不足 ({len(top_df)}件)。残りは補充が必要です")
    elif len(top_df) > TARGET_COUNT:
        top_df = top_df.head(TARGET_COUNT)

    # Pickle保存
    top_df.to_pickle(PKL_FILE)
    print(f"✅ symbols.pkl を更新しました: {len(top_df)}銘柄")


def run_symbol_update_loop():
    """スレッドで常駐実行"""
    schedule.every(5).minutes.do(update_symbols)
    update_symbols()  # 起動直後に一回実行

    while True:
        schedule.run_pending()
        time.sleep(5)


def start_symbol_updater_thread():
    """main.py から呼び出す関数"""
    t = threading.Thread(target=run_symbol_update_loop, daemon=True)
    t.start()
    print("⏳ シンボル自動更新スレッドを起動しました")
