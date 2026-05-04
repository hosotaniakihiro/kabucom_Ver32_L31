import os
import re
import sqlite3
import pandas as pd
from datetime import datetime
from glob import glob
from ta.momentum import RSIIndicator, StochasticOscillator

input_dir = r"Y:\y_stock_data_price"
output_dir = input_dir
input_files = sorted(glob(os.path.join(input_dir, "y_summary2025*.db")))

for input_path in input_files:
    print(f"\n📦 処理開始: {os.path.basename(input_path)}")

    conn = sqlite3.connect(input_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    table_names = [row[0] for row in cursor.fetchall()]
    print(f"📋 テーブル一覧: {table_names}")

    if "stock_summary" not in table_names:
        print("❌ 'stock_summary' テーブルが存在しないためスキップ")
        conn.close()
        continue

    df = pd.read_sql_query("SELECT * FROM stock_summary", conn)
    conn.close()

    print(f"📊 データ件数: {len(df)}")

    if len(df) == 0:
        print("⚠ データが空です。スキップ")
        continue

    if "date" in df.columns:
        df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.date
    elif "time_range" in df.columns:
        print("📌 'time_range' の先頭5件:", df['time_range'].head().tolist())
        df['date'] = df['time_range'].str.extract(r'(\d{4}[-/]\d{2}[-/]\d{2})')[0]
        df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.date
    else:
        print("❌ 'date' または 'time_range' 列が見つかりません。スキップ")
        continue

    if df['date'].isnull().all():
        print("❌ 'date' 抽出に失敗。日付データなし")
        continue

    # === 出力 ===
    for date, group in df.groupby('date'):
        if pd.isnull(date):
            continue

        # === テクニカル指標追加 ===
        if {'close_price', 'high_price', 'low_price'}.issubset(group.columns):
            try:
                group = group.sort_values(by='time_range' if 'time_range' in group.columns else 'id')

                rsi_calc = RSIIndicator(close=group['close_price'], window=14)
                group['rsi'] = rsi_calc.rsi()

                stoch = StochasticOscillator(
                    high=group['high_price'],
                    low=group['low_price'],
                    close=group['close_price'],
                    window=14,
                    smooth_window=3
                )
                group['slowk'] = stoch.stoch()
                group['slowd'] = stoch.stoch_signal()
            except Exception as e:
                print(f"⚠ テクニカル指標計算中にエラー: {e}")
        else:
            print("⚠ テクニカル指標に必要なカラムが存在しません。スキップ")

        date_str = date.strftime("%Y%m%d")
        output_path = os.path.join(output_dir, f"summary{date_str}.db")
        print(f"  ▶ 出力: {os.path.basename(output_path)} ({len(group)}件)")

        conn_out = sqlite3.connect(output_path)
        group.drop(columns=["date"], inplace=True, errors="ignore")
        group.to_sql("stock_summary", conn_out, index=False, if_exists="replace")
        conn_out.close()

print("\n✅ 分割処理が完了しました。")
