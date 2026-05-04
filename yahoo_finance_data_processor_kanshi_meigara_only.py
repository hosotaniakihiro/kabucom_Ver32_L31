# ============================================================
# yahoo_finance_data_processor_kanshi_meigara_only.py
# ------------------------------------------------------------
# - 監視銘柄のみ Yahoo 5分足を取得
# - ローカルDBとマージし、指標付きSQLiteとして保存
# - Ver16.1 用に最適化
# ============================================================

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

from symbol_loader import load_symbol_flags_df
import numpy as np


# --- 設定 ---
OUTPUT_BASE_DIR = r"Y:\y_stock_data_price"
DAYS_TO_FETCH_API = 1
DAYS_TO_LOAD_LOCAL = 3


# ============================================================
# テーブル作成
# ============================================================
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
        last_update TEXT,
        UNIQUE(symbol, date, time_range)
    );
    """
    conn.execute(create_table_sql)
    conn.commit()


# ============================================================
# 5分足 Yahoo API 取得
# ============================================================
def get_data_batch(symbol, start_ts, end_ts, interval='5m'):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={int(start_ts)}&period2={int(end_ts)}&interval={interval}"
    )

    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()

        js = r.json()
        result = js["chart"]["result"][0]

        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]

        df = pd.DataFrame({
            "time_range": pd.to_datetime(timestamps, unit='s') + pd.Timedelta(hours=9),
            "open_price": quote["open"],
            "high_price": quote["high"],
            "low_price": quote["low"],
            "close_price": quote["close"],
            "volume": quote["volume"]
        })

        df = df.dropna()

        # 昼休みを除外
        df = df[
            ~((df["time_range"].dt.time >= dt.time(11, 30))
              & (df["time_range"].dt.time < dt.time(12, 30)))
        ]

        df["time_range"] = df["time_range"].dt.strftime("%Y-%m-%d %H:%M:%S")
        return df

    except Exception as e:
        print(f"❌ Yahoo API取得エラー ({symbol}): {e}")
        return pd.DataFrame()


# ============================================================
# ローカルDBから 5m の履歴を取得
# ============================================================
def load_local_data(code, base_dir, days, table='stock_summary'):
    all_data = pd.DataFrame()
    today = dt.date.today()

    for i in range(days + 5):
        date_str = (today - dt.timedelta(days=i)).strftime('%Y%m%d')
        db_path = os.path.join(base_dir, f"summary{date_str}.db")

        if not os.path.exists(db_path):
            continue

        try:
            conn = sqlite3.connect(db_path)
            q = f"SELECT * FROM {table} WHERE symbol='{code}'"
            df = pd.read_sql_query(q, conn)
            conn.close()

            if not df.empty:
                all_data = pd.concat([all_data, df])
        except Exception as e:
            print(f"⚠ ローカル読み込みエラー ({code}): {e}")
            traceback.print_exc()

    if not all_data.empty:
        all_data["time_range"] = pd.to_datetime(all_data["time_range"])
        all_data = (
            all_data.sort_values("time_range")
            .drop_duplicates("time_range", keep="last")
        )

        all_data = all_data[
            all_data["time_range"] >= (dt.datetime.now() - dt.timedelta(days=days))
        ]

    return all_data


# ============================================================
# 指標計算
# ============================================================
def calculate_moving_averages(df):
    df["ma5"] = df["close_price"].rolling(5).mean()
    df["ma25"] = df["close_price"].rolling(25).mean()
    df["ma75"] = df["close_price"].rolling(75).mean()
    return df


def calculate_macd(df):
    df["ema12"] = df["close_price"].ewm(span=12, adjust=False).mean()
    df["ema26"] = df["close_price"].ewm(span=26, adjust=False).mean()
    df["macd"] = df["ema12"] - df["ema26"]
    df["signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    return df


def calculate_rsi_stoch(df):
    try:
        df["rsi"] = RSIIndicator(df["close_price"], window=14).rsi()
        so = StochasticOscillator(
            high=df["high_price"],
            low=df["low_price"],
            close=df["close_price"],
            window=14,
            smooth_window=3
        )
        df["slowk"] = so.stoch()
        df["slowd"] = so.stoch_signal()
    except Exception as e:
        print(f"⚠ RSI/Stoch計算エラー: {e}")
    return df


def calculate_rci(df, period=9):
    rci_vals = []
    closes = df["close_price"].to_list()

    for i in range(len(df)):
        if i < period - 1:
            rci_vals.append(None)
            continue

        win = closes[i - period + 1:i + 1]
        win_ser = pd.Series(win)

        date_rank = np.arange(1, period + 1)
        price_rank = win_ser.rank(method="first").to_numpy()

        d = date_rank - price_rank
        rci = (1 - (6 * (d ** 2).sum()) / (period * (period**2 - 1))) * 100
        rci_vals.append(rci)

    df["rci"] = rci_vals
    return df


def calculate_vwap(df):
    df["cum_pv"] = (df["close_price"] * df["volume"]).cumsum()
    df["cum_vol"] = df["volume"].cumsum()
    df["vwap"] = df["cum_pv"] / df["cum_vol"]
    df.drop(columns=["cum_pv", "cum_vol"], inplace=True)
    return df


def calculate_bollinger_bands(df):
    try:
        bb = BollingerBands(close=df["close_price"], window=20, window_dev=2)
        df["bb_mavg"] = bb.bollinger_mavg()
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
    except Exception as e:
        print(f"⚠ ボリンジャーバンド計算エラー: {e}")
    return df


# ============================================================
# SQLite へ保存
# ============================================================
def save_to_sqlite_daily(db_path, df, code, name, table="stock_summary"):

    try:
        df["symbol"] = code
        df["symbolname"] = name
        df["date"] = pd.to_datetime(df["time_range"]).dt.strftime("%Y-%m-%d")

        allowed = [
            "time_range", "symbol", "symbolname",
            "open_price", "high_price", "low_price", "close_price",
            "volume", "ma5", "ma25", "ma75", "ema12", "ema26",
            "macd", "signal", "rsi", "rci",
            "slowk", "slowd", "vwap",
            "bb_mavg", "bb_upper", "bb_lower",
            "date"
        ]

        df = df[[c for c in df.columns if c in allowed]]

        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)

        create_stock_summary_table_if_not_exists(conn, table)

        date_str = df["date"].iloc[0]
        conn.execute(f"DELETE FROM {table} WHERE symbol=? AND date=?", (code, date_str))

        df.to_sql(table, conn, if_exists="append", index=False)
        conn.close()

    except Exception as e:
        print(f"❌ SQLite保存エラー ({code}): {e}")
        traceback.print_exc()


# ============================================================
# メイン処理
# ============================================================
def process_and_save_all_stock_data_batch(output_base_dir, days_to_fetch_api, days_to_load_local):

    df_symbols = load_symbol_flags_df()

    if df_symbols is None or df_symbols.empty:
        print("❌ 銘柄Excelが読み込めません。")
        return

    print("📋 読み込んだ列:", df_symbols.columns.tolist())

    # Excel カラム対応
    if "symbol" in df_symbols.columns:
        pass
    elif "コード" in df_symbols.columns:
        df_symbols["symbol"] = df_symbols["コード"]
    else:
        raise ValueError("❌ Excelに 'symbol' または 'コード' 列がありません。")

    if "symbolname" in df_symbols.columns:
        pass
    elif "銘柄名" in df_symbols.columns:
        df_symbols["symbolname"] = df_symbols["銘柄名"]
    else:
        df_symbols["symbolname"] = df_symbols["symbol"]

    end_dt = dt.datetime.now().replace(hour=15, minute=30)
    start_dt = (end_dt - dt.timedelta(days=days_to_fetch_api)).replace(hour=9, minute=0)

    print(f"\n📈 対象銘柄数: {len(df_symbols)}件")
    print(f"期間: {start_dt:%Y-%m-%d} ～ {end_dt:%Y-%m-%d}\n")

    for idx, row in df_symbols.iterrows():
        code = str(row["symbol"])
        name = str(row["symbolname"])

        yahoo_symbol = f"{code}.T"
        print(f"\n▶ 処理中: {code} {name}")

        local_df = load_local_data(code, output_base_dir, days_to_load_local)
        api_df = get_data_batch(yahoo_symbol, start_dt.timestamp(), end_dt.timestamp(), interval="5m")

        if local_df.empty and api_df.empty:
            print("  ⚠ データなし → スキップ")
            continue

        if not local_df.empty:
            local_df["time_range"] = pd.to_datetime(local_df["time_range"])

        if not api_df.empty:
            api_df["time_range"] = pd.to_datetime(api_df["time_range"])

        df_all = pd.concat([local_df, api_df], ignore_index=True).drop_duplicates("time_range")
        df_all = df_all.sort_values("time_range")

        try:
            df_all = calculate_moving_averages(df_all)
            df_all = calculate_macd(df_all)
            df_all = calculate_rsi_stoch(df_all)
            df_all = calculate_rci(df_all)
            df_all = calculate_vwap(df_all)
            df_all = calculate_bollinger_bands(df_all)

            df_all["time_range"] = df_all["time_range"].dt.strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            print(f"⚠ 指標計算エラー: {e}")
            traceback.print_exc()
            continue

        # 日ごとに保存
        for i in range(days_to_load_local):
            target_date = (end_dt.date() - dt.timedelta(days=i))
            daily = df_all[pd.to_datetime(df_all["time_range"]).dt.date == target_date]

            if not daily.empty:
                db_path = os.path.join(
                    output_base_dir,
                    f"summary{target_date:%Y%m%d}.db"
                )
                save_to_sqlite_daily(db_path, daily.copy(), code, name)

        print(f"✅ 完了: {code} {name}")


# ============================================================
# 実行
# ============================================================
if __name__ == "__main__":
    os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
    process_and_save_all_stock_data_batch(OUTPUT_BASE_DIR, DAYS_TO_FETCH_API, DAYS_TO_LOAD_LOCAL)
