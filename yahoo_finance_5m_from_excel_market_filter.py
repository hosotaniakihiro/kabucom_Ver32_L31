# ============================================================
# yahoo_finance_5m_from_excel_59days_threadpool.py（Ver4.1）
# ------------------------------------------------------------
# ・Excelから市場区分フィルタ
# ・59日分の日別DB summary_ALLYYYYMMDD.db
# ・Yahoo 5m取得（欠損はskip）
# ・models.py互換UPSERT
# ・ThreadPoolExecutorで高速化
# ・DBロック時は自動リトライ（database is locked 対策）
# ・PRIMARY KEY(symbol, date, time_range)
# ============================================================

import os
import sqlite3
import pandas as pd
import numpy as np
import datetime as dt
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD
from ta.volatility import BollingerBands


# ============================================================
# 設定
# ============================================================
EXCEL_PATH = r"Y:\kabu\data_j.xls"
DB_ROOT = r"Y:\stock_price_data"

VALID_MARKETS = [
    "プライム（内国株式）",
    "スタンダード（内国株式）",
    "グロース（内国株式）",
]


# ============================================================
# DBテーブル作成（PRIMARY KEY付き）
# ============================================================
def create_summary_5min_table(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    sql = """
    CREATE TABLE IF NOT EXISTS stock_summary_5min (
        symbol TEXT NOT NULL,
        symbolname TEXT,
        date TEXT NOT NULL,
        time_range TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        time TEXT,
        open_price REAL,
        high_price REAL,
        low_price REAL,
        close_price REAL,
        volume REAL,
        vwap REAL,
        ma5 REAL,
        ma25 REAL,
        ma75 REAL,
        ema12 REAL,
        ema26 REAL,
        macd REAL,
        signal REAL,
        rsi REAL,
        rci REAL,
        bb_upper REAL,
        bb_lower REAL,
        bb_upper_3 REAL,
        bb_lower_3 REAL,
        slowk REAL,
        slowd REAL,
        atr REAL,
        last_update TEXT,
        PRIMARY KEY(symbol, date, time_range)
    );
    """
    cur.execute(sql)
    conn.commit()
    conn.close()


# ============================================================
# Yahoo 5m データ取得
# ============================================================
def fetch_5min(symbol, start_ts, end_ts):

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={int(start_ts)}&period2={int(end_ts)}&interval=5m"
    )

    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        data = r.json()

        if data["chart"].get("result") is None:
            print(f"⚠ result=None → skip {symbol}")
            return pd.DataFrame()

        js = data["chart"]["result"][0]

        if "timestamp" not in js or js["timestamp"] is None:
            print(f"⚠ timestamp欠損 → skip {symbol}")
            return pd.DataFrame()

        ts = js["timestamp"]
        ind = js["indicators"]["quote"][0]

        df = pd.DataFrame({
            "datetime": pd.to_datetime(ts, unit="s") + pd.Timedelta(hours=9),
            "open_price": ind["open"],
            "high_price": ind["high"],
            "low_price": ind["low"],
            "close_price": ind["close"],
            "volume": ind["volume"],
        }).dropna()

        return df

    except Exception as e:
        print(f"❌ Yahoo失敗 {symbol}: {e}")
        return pd.DataFrame()


# ============================================================
# 指標計算（models.py準拠）
# ============================================================
def calc_indicators(df):
    df = df.copy()

    df["ma5"] = df["close_price"].rolling(5).mean()
    df["ma25"] = df["close_price"].rolling(25).mean()
    df["ma75"] = df["close_price"].rolling(75).mean()

    df["ema12"] = df["close_price"].ewm(span=12).mean()
    df["ema26"] = df["close_price"].ewm(span=26).mean()
    df["macd"] = df["ema12"] - df["ema26"]
    df["signal"] = df["macd"].ewm(span=9).mean()

    try:
        df["rsi"] = RSIIndicator(df["close_price"], window=14).rsi()
    except:
        df["rsi"] = None

    try:
        sto = StochasticOscillator(
            high=df["high_price"], low=df["low_price"],
            close=df["close_price"], window=14, smooth_window=3
        )
        df["slowk"] = sto.stoch()
        df["slowd"] = sto.stoch_signal()
    except:
        df["slowk"] = None
        df["slowd"] = None

    # RCI
    period = 9
    rci_vals = []
    closes = df["close_price"].tolist()
    for i in range(len(df)):
        if i < period - 1:
            rci_vals.append(None)
        else:
            win = closes[i - period + 1:i + 1]
            ser = pd.Series(win)
            d = np.arange(1, period + 1) - ser.rank().to_numpy()
            rci_vals.append((1 - (6 * (d ** 2).sum()) /
                             (period * (period**2 - 1))) * 100)
    df["rci"] = rci_vals

    # VWAP
    df["cum_pv"] = (df["close_price"] * df["volume"]).cumsum()
    df["cum_v"] = df["volume"].cumsum()
    df["vwap"] = df["cum_pv"] / df["cum_v"]
    df.drop(columns=["cum_pv", "cum_v"], inplace=True)

    # BB
    try:
        bb = BollingerBands(df["close_price"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
    except:
        df["bb_upper"] = None
        df["bb_lower"] = None

    df["bb_upper_3"] = None
    df["bb_lower_3"] = None
    df["atr"] = None
    df["last_update"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return df


# ============================================================
# UPSERT SQL
# ============================================================
TABLE_COLUMNS = [
    "symbol","symbolname","date","time_range","start_time","end_time","time",
    "open_price","high_price","low_price","close_price","volume","vwap",
    "ma5","ma25","ma75","ema12","ema26","macd","signal","rsi","rci",
    "bb_upper","bb_lower","bb_upper_3","bb_lower_3","slowk","slowd","atr",
    "last_update"
]


def build_upsert_sql():
    cols = ",".join(TABLE_COLUMNS)
    ph = ",".join(["?"] * len(TABLE_COLUMNS))
    update = ",".join([f"{c}=excluded.{c}" for c in TABLE_COLUMNS
                       if c not in ("symbol", "date", "time_range")])

    return f"""
    INSERT INTO stock_summary_5min ({cols})
    VALUES ({ph})
    ON CONFLICT(symbol, date, time_range)
    DO UPDATE SET {update};
    """


# ============================================================
# SQLite ロック対策：自動リトライ
# ============================================================
def execute_with_retry(cur, sql, row, retries=5):
    for i in range(retries):
        try:
            cur.execute(sql, row)
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e):
                time.sleep(0.1 * (i+1))
                continue
            raise
    raise RuntimeError("DB locked - retry failed")


# ============================================================
# ThreadPool 用：1銘柄処理
# ============================================================
def save_one_symbol(db_path, symbol, name, start_ts, end_ts):

    yahoo_symbol = f"{symbol}.T"

    df = fetch_5min(yahoo_symbol, start_ts, end_ts)
    if df.empty:
        return f"skip {symbol}"

    df = calc_indicators(df)

    df["date"] = df["datetime"].dt.date.astype(str)
    df["time"] = df["datetime"].dt.strftime("%H:%M:%S")
    df["start_time"] = df["datetime"].dt.strftime("%H:%M")
    df["end_time"] = (df["datetime"] + pd.Timedelta(minutes=5)).dt.strftime("%H:%M")
    df["time_range"] = df["start_time"] + " - " + df["end_time"]

    df["symbol"] = symbol
    df["symbolname"] = name

    df.drop(columns=["datetime"], inplace=True)

    for col in TABLE_COLUMNS:
        if col not in df.columns:
            df[col] = None

    df = df[TABLE_COLUMNS]

    sql = build_upsert_sql()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    for row in df.itertuples(index=False, name=None):
        execute_with_retry(cur, sql, row)  # 🔥 ロック対策

    conn.commit()
    conn.close()

    return f"ok {symbol}"


# ============================================================
# メイン処理（59日 × ThreadPool）
# ============================================================
def run():

    df_raw = pd.read_excel(EXCEL_PATH)
    df = df_raw[df_raw["市場・商品区分"].isin(VALID_MARKETS)]

    symbols = df["コード"].astype(str).str.zfill(4).tolist()
    names   = df["銘柄名"].tolist()

    today = dt.date.today()
    start_date = today - dt.timedelta(days=59)

    print(f"📌 対象銘柄：{len(symbols)}")
    print(f"📅 期間：{start_date} 〜 {today}")

    for current_date in pd.date_range(start=start_date, end=today):

        ymd = current_date.date()
        print(f"\n====================")
        print(f"📅 日付：{ymd}")
        print(f"====================")

        db_path = os.path.join(DB_ROOT, f"summary_ALL{ymd:%Y%m%d}.db")

        # テーブル生成
        create_summary_5min_table(db_path)

        start_dt = dt.datetime(ymd.year, ymd.month, ymd.day, 9, 0)
        end_dt   = dt.datetime(ymd.year, ymd.month, ymd.day, 15, 30)
        start_ts = start_dt.timestamp()
        end_ts   = end_dt.timestamp()

        # ThreadPool
        with ThreadPoolExecutor(max_workers=32) as exe:
            futures = [
                exe.submit(save_one_symbol, db_path, symbol, name, start_ts, end_ts)
                for symbol, name in zip(symbols, names)
            ]

            for f in as_completed(futures):
                print(f.result())

    print("\n🎉 59日分 全データダウンロード完了！")


if __name__ == "__main__":
    run()
