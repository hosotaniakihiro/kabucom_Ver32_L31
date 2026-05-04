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

# --- 設定 ---
OUTPUT_BASE_DIR = r"Y:\y_stock_data_price"
EXCEL_FILE_PATH = r"y:\kabu\data_j.xls"   # ← ここを固定
DAYS_TO_FETCH_API = 59
DAYS_TO_LOAD_LOCAL = 59


def create_stock_summary_table_if_not_exists(conn, table_name='stock_summary'):
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
        last_update,
        UNIQUE(symbol, date, time_range) 
    );
    """
    conn.execute(create_table_sql)
    conn.commit()


def calculate_moving_averages(df):
    df_copy = df.copy()
    df_copy['ma5'] = df_copy['close_price'].rolling(window=5, min_periods=1).mean()
    df_copy['ma25'] = df_copy['close_price'].rolling(window=25, min_periods=1).mean()
    df_copy['ma75'] = df_copy['close_price'].rolling(window=75, min_periods=1).mean()
    return df_copy


def calculate_macd(df, short_window=12, long_window=26, signal_window=9):
    df_copy = df.copy()
    df_copy['ema12'] = df_copy['close_price'].ewm(span=short_window, adjust=False).mean()
    df_copy['ema26'] = df_copy['close_price'].ewm(span=long_window, adjust=False).mean()
    df_copy['macd'] = df_copy['ema12'] - df_copy['ema26']
    df_copy['signal'] = df_copy['macd'].ewm(span=signal_window, adjust=False).mean()
    return df_copy


def calculate_rsi_stoch(df):
    df_copy = df.copy()
    try:
        rsi_calc = RSIIndicator(close=df_copy['close_price'], window=14)
        df_copy['rsi'] = rsi_calc.rsi()
        stoch = StochasticOscillator(
            high=df_copy['high_price'],
            low=df_copy['low_price'],
            close=df_copy['close_price'],
            window=14,
            smooth_window=3
        )
        df_copy['slowk'] = stoch.stoch()
        df_copy['slowd'] = stoch.stoch_signal()
    except Exception as e:
        print(f"⚠ RSI/Stoch計算エラー: {e}")
        traceback.print_exc()
    return df_copy


def calculate_bollinger_bands(df):
    df_copy = df.copy()
    try:
        bb = BollingerBands(close=df_copy['close_price'], window=20, window_dev=2)
        df_copy['bb_mavg'] = bb.bollinger_mavg().ffill()
        df_copy['bb_upper'] = bb.bollinger_hband().ffill()
        df_copy['bb_lower'] = bb.bollinger_lband().ffill()
    except Exception as e:
        print(f"⚠ ボリンジャーバンド計算エラー: {e}")
        traceback.print_exc()
    return df_copy


def calculate_vwap(df):
    df_copy = df.copy()
    try:
        df_copy['cum_pv'] = (df_copy['close_price'] * df_copy['volume']).cumsum()
        df_copy['cum_volume'] = df_copy['volume'].cumsum()
        df_copy['vwap'] = df_copy['cum_pv'] / df_copy['cum_volume']
        df_copy.drop(columns=['cum_pv', 'cum_volume'], inplace=True)
    except Exception as e:
        print(f"⚠ VWAP計算中にエラー: {e}")
        traceback.print_exc()
    return df_copy


def calculate_rci(df, period=9):
    df_copy = df.copy()
    try:
        rci_values = []
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
    except Exception as e:
        print(f"⚠ RCI({period}) 計算エラー: {e}")
        traceback.print_exc()
    return df_copy


def get_data_batch(symbol, start_ts, end_ts, interval='5m'):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={int(start_ts)}&period2={int(end_ts)}&interval={interval}"
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        data = response.json()
        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        timestamps = result['timestamp']
        df = pd.DataFrame({
            'time_range': pd.to_datetime(timestamps, unit='s') + pd.Timedelta(hours=9),
            'open_price': quote['open'],
            'high_price': quote['high'],
            'low_price': quote['low'],
            'close_price': quote['close'],
            'volume': quote['volume']
        })
        df = df.dropna()
        df = df[~((df['time_range'].dt.time >= dt.time(11, 30)) & (df['time_range'].dt.time < dt.time(12, 30)))]
        df['time_range'] = df['time_range'].dt.strftime("%Y-%m-%d %H:%M:%S")
        return df
    except Exception as e:
        print(f"❌ API取得エラー ({symbol}): {e}")
        return pd.DataFrame()


def save_to_sqlite_daily(db_path, df, code, name, table='stock_summary'):
    try:
        df['symbol'] = code
        df['symbolname'] = name
        df['date'] = pd.to_datetime(df['time_range']).dt.strftime('%Y-%m-%d')
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        create_stock_summary_table_if_not_exists(conn, table)
        conn.execute(f"DELETE FROM {table} WHERE symbol = ? AND date = ?", (code, df['date'].iloc[0]))
        df.to_sql(table, conn, if_exists='append', index=False)
        conn.close()
        print(f"✅ 保存成功 {code} {name} -> {os.path.basename(db_path)}")
    except Exception as e:
        print(f"❌ SQLite保存エラー ({code}): {e}")
        traceback.print_exc()


def process_and_save_all_stock_data_batch(excel_file_path, output_base_dir, days_to_fetch_api, days_to_load_local):
    try:
        df_excel = pd.read_excel(excel_file_path)
        valid_markets = ["プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"]
        df_excel = df_excel[df_excel['市場・商品区分'].isin(valid_markets)]
    except Exception as e:
        print(f"❌ Excel読み込みエラー: {e}")
        return

    end_dt = dt.datetime.now().replace(hour=15, minute=30)
    start_dt = (end_dt - dt.timedelta(days=days_to_fetch_api)).replace(hour=9, minute=0)

    for idx, row in df_excel.iterrows():
        code = str(row['コード']).zfill(4)
        name = row['銘柄名']
        yahoo_symbol = f"{code}.T"
        print(f"🚀 {name}({code}) 取得開始")

        api_df = get_data_batch(yahoo_symbol, start_dt.timestamp(), end_dt.timestamp(), interval='5m')
        if api_df.empty:
            print(f"⚠ {name}({code}) データなし")
            continue

        combined_df = api_df.copy()
        combined_df = calculate_moving_averages(combined_df)
        combined_df = calculate_macd(combined_df)
        combined_df = calculate_rsi_stoch(combined_df)
        combined_df = calculate_rci(combined_df)
        combined_df = calculate_vwap(combined_df)
        combined_df = calculate_bollinger_bands(combined_df)
        combined_df['time_range'] = pd.to_datetime(combined_df['time_range']).dt.strftime("%Y-%m-%d %H:%M:%S")

        for single_date in combined_df['time_range'].str[:10].unique():
            daily = combined_df[pd.to_datetime(combined_df['time_range']).dt.strftime("%Y-%m-%d") == single_date]
            if not daily.empty:
                db_path = os.path.join(output_base_dir, f"summary{single_date.replace('-', '')}.db")
                save_to_sqlite_daily(db_path, daily.copy(), code, name)

        print(f"✅ 完了: {code} {name}")


# --- メイン ---
if __name__ == "__main__":
    os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
    process_and_save_all_stock_data_batch(EXCEL_FILE_PATH, OUTPUT_BASE_DIR, DAYS_TO_FETCH_API, DAYS_TO_LOAD_LOCAL)
