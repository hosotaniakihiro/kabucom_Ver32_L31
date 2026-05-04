import time
import sys
import pandas as pd
import requests
import os
import sqlite3
import datetime as dt
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD
from ta.volatility import BollingerBands
import traceback

# --- 設定 ---
OUTPUT_BASE_DIR = r"Y:\y_stock_data_price"
EXCEL_FILE_PATH = r"y:\kabu\data_j.xls"
DAYS_TO_FETCH_API = 59  # APIから取得する日数
DAYS_TO_LOAD_LOCAL = 3    # ローカルから読み込む日数


# =========================================================
# DB テーブル作成
# =========================================================
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
        value INTEGER,
        ma5 REAL,
        ma25 REAL,
        ma75 REAL,
        ema12 REAL,
        ema26 REAL,
        macd REAL,
        signal REAL,
        rsi REAL,
        slowk REAL,
        slowd REAL,
        vwap REAL,
        bb_mavg REAL,
        bb_upper REAL,
        bb_lower REAL,
        date TEXT
    );
    """
    conn.execute(create_table_sql)
    conn.commit()


# =========================================================
# テクニカル指標計算
# =========================================================
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
    if {'close_price', 'high_price', 'low_price'}.issubset(df_copy.columns):
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
    return df_copy

def calculate_bollinger_bands(df):
    df_copy = df.copy()
    bb = BollingerBands(close=df_copy['close_price'], window=20, window_dev=2)
    df_copy['bb_mavg'] = bb.bollinger_mavg().ffill()
    df_copy['bb_upper'] = bb.bollinger_hband().ffill()
    df_copy['bb_lower'] = bb.bollinger_lband().ffill()
    return df_copy

def calculate_vwap(df):
    df_copy = df.copy()
    df_copy['cum_pv'] = (df_copy['close_price'] * df_copy['volume']).cumsum()
    df_copy['cum_volume'] = df_copy['volume'].cumsum()
    df_copy['vwap'] = df_copy['cum_pv'] / df_copy['cum_volume']
    df_copy.drop(columns=['cum_pv', 'cum_volume'], inplace=True)
    return df_copy


# =========================================================
# Yahoo Finance API データ取得
# =========================================================
def get_data_batch(symbol, start_date_ts, end_date_ts, interval='5m'):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={int(start_date_ts)}&period2={int(end_date_ts)}&interval={interval}"
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        data = response.json()
        if 'chart' not in data or not data['chart']['result']:
            return pd.DataFrame()

        result = data['chart']['result'][0]
        quote = result['indicators']['quote'][0]
        timestamps = result.get('timestamp', [])
        if not timestamps:
            return pd.DataFrame()

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
        print(f"⚠ API取得エラー: {e}")
        return pd.DataFrame()


# =========================================================
# ローカルデータ読込
# =========================================================
def load_local_data(stock_code, output_base_dir, days_to_load, table_name='stock_summary'):
    all_local_data = pd.DataFrame()
    code_str = str(stock_code).zfill(4)
    today = dt.date.today()
    for i in range(days_to_load + 5):
        target_date = today - dt.timedelta(days=i)
        db_path = os.path.join(output_base_dir, f"summary{target_date.strftime('%Y%m%d')}.db")
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                query = f"SELECT * FROM {table_name} WHERE symbol = '{code_str}' ORDER BY time_range ASC"
                daily_df = pd.read_sql_query(query, conn)
                conn.close()
                if not daily_df.empty:
                    all_local_data = pd.concat([all_local_data, daily_df], ignore_index=True)
            except:
                continue
    return all_local_data


# =========================================================
# 保存処理（日ごと＋月ごと）
# =========================================================
def save_to_sqlite_daily(db_path, data_df_single_day, stock_code, stock_name, table_name='stock_summary'):
    conn = sqlite3.connect(db_path)
    create_stock_summary_table_if_not_exists(conn, table_name)
    cursor = conn.cursor()
    delete_date = data_df_single_day['date'].iloc[0]
    cursor.execute(f"DELETE FROM {table_name} WHERE symbol = ? AND date = ?",
                   (str(stock_code).zfill(4), delete_date))
    conn.commit()
    data_df_single_day.to_sql(table_name, conn, if_exists='append', index=False)
    conn.close()

def save_to_sqlite_daily_and_monthly(output_base_dir, data_df_single_day, stock_code, stock_name):
    date_str = data_df_single_day['date'].iloc[0]
    daily_db_path   = os.path.join(output_base_dir, f"summary{date_str.replace('-', '')}.db")
    monthly_db_path = os.path.join(output_base_dir, f"summary{date_str[:7].replace('-', '')}.db")
    table_name_daily   = "stock_summary"
    table_name_monthly = f"stock_summary_{date_str[:7].replace('-', '')}"

    # ---- 日ごとDB ----
    save_to_sqlite_daily(daily_db_path, data_df_single_day, stock_code, stock_name, table_name_daily)

    # ---- 月ごとDB ----
    try:
        conn_month = sqlite3.connect(monthly_db_path)
        create_stock_summary_table_if_not_exists(conn_month, table_name_monthly)
        cursor = conn_month.cursor()

        cursor.execute(
            f"DELETE FROM {table_name_monthly} WHERE symbol = ? AND date = ?",
            (str(stock_code).zfill(4), date_str)
        )
        conn_month.commit()

        # ✅ id列を削除（存在する場合）
        if 'id' in data_df_single_day.columns:
            data_df_single_day = data_df_single_day.drop(columns=['id'])

        data_df_single_day.to_sql(table_name_monthly, conn_month, if_exists="append", index=False)

        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_name_monthly}_symbol_date "
            f"ON {table_name_monthly}(symbol, date)"
        )
        conn_month.commit()
        conn_month.close()
        print(f"  ✅ {stock_code} {stock_name} を {os.path.basename(monthly_db_path)} の {table_name_monthly} に追加しました。")

    except Exception as e:
        print(f"❌ 月次DB保存エラー ({stock_code}): {e}")
        traceback.print_exc()


# =========================================================
# メイン処理
# =========================================================
def process_and_save_all_stock_data_batch(excel_file_path, output_base_dir, days_to_fetch_api, days_to_load_local):
    df_excel = pd.read_excel(excel_file_path)
    valid_markets = ["プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"]
    df_excel = df_excel[df_excel['市場・商品区分'].isin(valid_markets)]

    total_symbols = len(df_excel)
    print(f"📊 対象銘柄数: {total_symbols}")

    end_date_for_api = dt.datetime.now().replace(hour=15, minute=0, second=0)
    start_date_for_api = (end_date_for_api - dt.timedelta(days=days_to_fetch_api)).replace(hour=9, minute=0, second=0)

    for idx, row in enumerate(df_excel.itertuples(), start=1):
        code = row.コード
        name = row.銘柄名
        yahoo_symbol = f"{code}.T"

        print(f"\n[{idx}/{total_symbols}] 🚀 {name} ({code}) の処理開始")

        # ローカルデータ読込
        local_data_df = load_local_data(code, output_base_dir, days_to_load_local)

        # APIデータ取得
        api_data_df = get_data_batch(yahoo_symbol, start_date_for_api.timestamp(), end_date_for_api.timestamp(), interval='5m')

        combined_df = pd.concat([local_data_df, api_data_df], ignore_index=True).drop_duplicates(subset=['time_range'])
        if combined_df.empty:
            print(f"⚠ {name} ({code}) データなし → スキップ")
            continue

        # テクニカル指標計算
        combined_df = combined_df.sort_values(by='time_range').reset_index(drop=True)
        combined_df = calculate_moving_averages(combined_df)
        combined_df = calculate_macd(combined_df)
        combined_df = calculate_rsi_stoch(combined_df)
        combined_df = calculate_bollinger_bands(combined_df)
        combined_df = calculate_vwap(combined_df)

        # 整形
        combined_df['time_range'] = pd.to_datetime(combined_df['time_range'])
        combined_df['date'] = combined_df['time_range'].dt.strftime("%Y-%m-%d")
        combined_df['time_range'] = combined_df['time_range'].dt.strftime("%Y-%m-%d %H:%M:%S")
        combined_df['symbol'] = str(code).zfill(4)
        combined_df['symbolname'] = name

        # 保存
        for single_date, daily_data_df in combined_df.groupby('date'):
            save_to_sqlite_daily_and_monthly(output_base_dir, daily_data_df, code, name)

        print(f"✅ {name} ({code}) 保存完了 ({idx}/{total_symbols})")


# =========================================================
# 実行
# =========================================================
if __name__ == "__main__":
    os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
    process_and_save_all_stock_data_batch(EXCEL_FILE_PATH, OUTPUT_BASE_DIR, DAYS_TO_FETCH_API, DAYS_TO_LOAD_LOCAL)
