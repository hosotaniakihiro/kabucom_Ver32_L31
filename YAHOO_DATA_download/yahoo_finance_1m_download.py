import time
import sys
import pandas as pd
import requests
import os
import json
import sqlite3
import datetime as dt
import traceback
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD
from ta.volatility import BollingerBands
import numpy as np  # ← RCI計算に必要
from convert_yahoo_to_summary import convert_all
# --- 設定 ---
OUTPUT_BASE_DIR = r"Y:\y_stock_data_price"
EXCEL_FILE_PATH = r"y:\kabu\data_j.xls"


# ============================================================
# DB作成
# ============================================================
def create_stock_summary_table_if_not_exists(conn, table_name='stock_summary_1min'):
    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_range TEXT,
        symbol TEXT,
        symbolname TEXT,
        open_price REAL,
        high_price REAL,
        low_price REAL,
        close_price REAL,
        volume INTEGER,
        ma5 REAL,
        ma25 REAL,
        ma75 REAL,
        ema12 REAL,
        ema26 REAL,
        macd REAL,
        signal REAL,
        rsi REAL,
        rci REAL,
        slowk REAL,
        slowd REAL,
        vwap REAL,
        bb_mavg REAL,
        bb_upper REAL,
        bb_lower REAL,
        date TEXT,
        last_update TEXT,
        UNIQUE(symbol, date, time_range)
    );
    """
    conn.execute(create_table_sql)
    conn.commit()


# ============================================================
# DB 自動アップグレード（足りないカラムは自動追加）
# ============================================================
def upgrade_stock_summary_table(conn, table_name="stock_summary"):
    required_cols = [
        "time_range","symbol","symbolname",
        "open_price","high_price","low_price","close_price","volume",
        "ma5","ma25","ma75",
        "ema12","ema26","macd","signal",
        "rsi","rci",
        "slowk","slowd",
        "vwap",
        "bb_mavg","bb_upper","bb_lower",
        "date","last_update"
    ]

    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    existing_cols = [row[1] for row in cur.fetchall()]

    for col in required_cols:
        if col not in existing_cols:
            print(f"⚙ DBにカラム追加: {col}")
            col_type = "REAL"
            if col in ("symbol", "symbolname", "date", "time_range", "last_update"):
                col_type = "TEXT"

            try:
                cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {col_type};")
            except Exception as e:
                print(f"❌ カラム追加失敗 {col}: {e}")

    conn.commit()


# ============================================================
# 指標計算（共通）
# ============================================================
def calc_indicators(df):
    df_copy = df.copy()

    # MA
    df_copy['ma5'] = df_copy['close_price'].rolling(window=5).mean()
    df_copy['ma25'] = df_copy['close_price'].rolling(window=25).mean()
    df_copy['ma75'] = df_copy['close_price'].rolling(window=75).mean()

    # MACD
    df_copy['ema12'] = df_copy['close_price'].ewm(span=12, adjust=False).mean()
    df_copy['ema26'] = df_copy['close_price'].ewm(span=26, adjust=False).mean()
    df_copy['macd'] = df_copy['ema12'] - df_copy['ema26']
    df_copy['signal'] = df_copy['macd'].ewm(span=9, adjust=False).mean()

    # RSI / ストキャス
    try:
        rsi_calc = RSIIndicator(close=df_copy['close_price'], window=14)
        df_copy['rsi'] = rsi_calc.rsi()

        stoch = StochasticOscillator(
            high=df_copy['high_price'],
            low=df_copy['low_price'],
            close=df_copy['close_price'],
            window=14,
            smooth_window=3,
        )
        df_copy['slowk'] = stoch.stoch()
        df_copy['slowd'] = stoch.stoch_signal()
    except:
        df_copy['rsi'] = None
        df_copy['slowk'] = None
        df_copy['slowd'] = None

    # RCI
    try:
        rci_values = []
        period = 9
        for i in range(len(df_copy)):
            if i < period - 1:
                rci_values.append(None)
                continue
            window = df_copy['close_price'].iloc[i - period + 1:i + 1].reset_index(drop=True)
            date_rank = np.arange(1, period + 1)
            price_rank = window.rank(method="first").to_numpy()
            d = date_rank - price_rank
            rci = (1 - (6 * np.sum(d ** 2)) / (period * (period ** 2 - 1))) * 100
            rci_values.append(rci)
        df_copy['rci'] = rci_values
    except:
        df_copy['rci'] = None

    # VWAP
    df_copy['cum_pv'] = (df_copy['close_price'] * df_copy['volume']).cumsum()
    df_copy['cum_volume'] = df_copy['volume'].cumsum()
    df_copy['vwap'] = df_copy['cum_pv'] / df_copy['cum_volume']
    df_copy.drop(columns=['cum_pv', 'cum_volume'], inplace=True)

    # ボリンジャーバンド
    try:
        bb = BollingerBands(close=df_copy['close_price'], window=20, window_dev=2)
        df_copy['bb_mavg'] = bb.bollinger_mavg().ffill()
        df_copy['bb_upper'] = bb.bollinger_hband().ffill()
        df_copy['bb_lower'] = bb.bollinger_lband().ffill()
    except:
        df_copy['bb_mavg'] = None
        df_copy['bb_upper'] = None
        df_copy['bb_lower'] = None

    return df_copy


# ============================================================
# Yahoo から1分足取得
# ============================================================
def get_1min_data(symbol):
    """
    Yahoo Financeの1分足は最大7日
    今回は 前日 + 当日の2日間 を取得
    """
    end_dt = dt.datetime.now()
    start_dt = end_dt - dt.timedelta(days=2)

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={int(start_dt.timestamp())}&period2={int(end_dt.timestamp())}"
        f"&interval=1m"
    )

    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        data = response.json()

        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        timestamps = result["timestamp"]

        df = pd.DataFrame({
            "time_range": pd.to_datetime(timestamps, unit="s") + pd.Timedelta(hours=9),
            "open_price": quote["open"],
            "high_price": quote["high"],
            "low_price": quote["low"],
            "close_price": quote["close"],
            "volume": quote["volume"],
        }).dropna()

        # 昼休み除外
        df = df[~((df["time_range"].dt.time >= dt.time(11, 30)) &
                  (df["time_range"].dt.time < dt.time(12, 30)))]

        df["time_range"] = df["time_range"].dt.strftime("%Y-%m-%d %H:%M:%S")
        return df

    except Exception as e:
        print(f"❌ API取得エラー: {symbol} - {e}")
        return pd.DataFrame()


# ============================================================
# DBへ日付ごと保存
# ============================================================
def save_daily(df, code, name, base_dir):
    df['symbol'] = code
    df['symbolname'] = name
    df['date'] = pd.to_datetime(df['time_range']).dt.strftime('%Y-%m-%d')
    df['last_update'] = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ★ time_range はそのまま使う（絶対に固定しない）
    # df['time_range'] = "1m"  ←これ削除！

    for single_date in df['date'].unique():
        daily = df[df['date'] == single_date].copy()
        if daily.empty:
            continue

        db_path = os.path.join(base_dir, f"summary{single_date.replace('-', '')}.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        conn = sqlite3.connect(db_path)
        create_stock_summary_table_if_not_exists(conn)
        upgrade_stock_summary_table(conn)

        # ★ 既存の全1分足レコードを削除
        conn.execute(
            "DELETE FROM stock_summary WHERE symbol = ? AND date = ?",
            (code, single_date)
        )

        daily.to_sql("stock_summary", conn, if_exists="append", index=False)
        conn.close()

        print(f"✔ 保存完了 {code} {name} → {os.path.basename(db_path)}")

# ============================================================
# メイン処理
# ============================================================
def process_all(excel_file_path, output_base_dir):
    try:
        df_excel = pd.read_excel(excel_file_path)
        df_excel = df_excel[df_excel["市場・商品区分"].isin(
            ["プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"]
        )]
    except Exception as e:
        print(f"❌ Excel読み込みエラー: {e}")
        return

    for _, row in df_excel.iterrows():
        code = str(row["コード"]).zfill(4)
        name = row["銘柄名"]
        yahoo_symbol = f"{code}.T"

        print(f"\n🚀 {name}({code}) 1分足取得開始")

        df1 = get_1min_data(yahoo_symbol)
        if df1.empty:
            print(f"⚠ 1分足データなし {code}")
            continue

        df1 = calc_indicators(df1)
        save_daily(df1, code, name, output_base_dir)

    print("\n🎉 全銘柄 完了しました！")


# --- メイン ---
if __name__ == "__main__":
    os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
    process_all(EXCEL_FILE_PATH, OUTPUT_BASE_DIR)
    convert_all()