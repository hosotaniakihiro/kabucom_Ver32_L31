import os
import sys
import sqlite3
import datetime as dt
import pandas as pd
import numpy as np
import requests
import jpholiday

from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands

from convert_yahoo_to_summary import convert_all


# ============================================================
# 設定
# ============================================================
OUTPUT_BASE_DIR = r"Y:\y_stock_data_price"
EXCEL_FILE_PATH = r"y:\kabu\data_j.xls"


# ============================================================
# 直近営業日取得（土日・祝日対応）
# ============================================================
def get_latest_trading_day(base_date=None):
    if base_date is None:
        base_date = dt.date.today()

    d = base_date
    while True:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5 and not jpholiday.is_holiday(d):
            return d


# ============================================================
# DB作成
# ============================================================
def create_stock_summary_table_if_not_exists(conn, table_name):
    sql = f"""
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
    conn.execute(sql)
    conn.commit()


def upgrade_stock_summary_table(conn, table_name):
    required_cols = [
        "time_range","symbol","symbolname",
        "open_price","high_price","low_price","close_price","volume",
        "ma5","ma25","ma75",
        "ema12","ema26","macd","signal",
        "rsi","rci","slowk","slowd",
        "vwap","bb_mavg","bb_upper","bb_lower",
        "date","last_update"
    ]

    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    existing = [r[1] for r in cur.fetchall()]

    for col in required_cols:
        if col not in existing:
            col_type = "REAL"
            if col in ("symbol","symbolname","date","time_range","last_update"):
                col_type = "TEXT"
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {col_type}")

    conn.commit()


# ============================================================
# 指標計算
# ============================================================
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ma5"] = df["close_price"].rolling(5).mean()
    df["ma25"] = df["close_price"].rolling(25).mean()
    df["ma75"] = df["close_price"].rolling(75).mean()

    df["ema12"] = df["close_price"].ewm(span=12, adjust=False).mean()
    df["ema26"] = df["close_price"].ewm(span=26, adjust=False).mean()
    df["macd"] = df["ema12"] - df["ema26"]
    df["signal"] = df["macd"].ewm(span=9, adjust=False).mean()

    try:
        df["rsi"] = RSIIndicator(df["close_price"], 14).rsi()
        st = StochasticOscillator(
            high=df["high_price"],
            low=df["low_price"],
            close=df["close_price"],
            window=14,
            smooth_window=3
        )
        df["slowk"] = st.stoch()
        df["slowd"] = st.stoch_signal()
    except:
        df["rsi"] = df["slowk"] = df["slowd"] = None

    # RCI
    try:
        period = 9
        rci_vals = []
        for i in range(len(df)):
            if i < period - 1:
                rci_vals.append(None)
                continue
            w = df["close_price"].iloc[i - period + 1:i + 1]
            date_rank = np.arange(1, period + 1)
            price_rank = w.rank(method="first").to_numpy()
            d = date_rank - price_rank
            rci = (1 - (6 * (d ** 2).sum()) / (period * (period ** 2 - 1))) * 100
            rci_vals.append(rci)
        df["rci"] = rci_vals
    except:
        df["rci"] = None

    pv = (df["close_price"] * df["volume"]).cumsum()
    vol = df["volume"].cumsum()
    df["vwap"] = pv / vol

    try:
        bb = BollingerBands(df["close_price"], window=20, window_dev=2)
        df["bb_mavg"] = bb.bollinger_mavg()
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
    except:
        df["bb_mavg"] = df["bb_upper"] = df["bb_lower"] = None

    return df


# ============================================================
# Yahoo Finance 1分足取得（直近2営業日分）
# ============================================================
def get_1min_data(symbol: str) -> pd.DataFrame:
    end_dt = dt.datetime.now()
    start_dt = end_dt - dt.timedelta(days=2)

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={int(start_dt.timestamp())}"
        f"&period2={int(end_dt.timestamp())}"
        f"&interval=1m"
    )

    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        js = r.json()["chart"]["result"][0]

        quote = js["indicators"]["quote"][0]
        ts = js["timestamp"]

        df = pd.DataFrame({
            "time_range": pd.to_datetime(ts, unit="s") + pd.Timedelta(hours=9),
            "open_price": quote["open"],
            "high_price": quote["high"],
            "low_price": quote["low"],
            "close_price": quote["close"],
            "volume": quote["volume"],
        }).dropna()

        df = df[~(
            (df["time_range"].dt.time >= dt.time(11, 30)) &
            (df["time_range"].dt.time < dt.time(12, 30))
        )]

        df["time_range"] = df["time_range"].dt.strftime("%Y-%m-%d %H:%M:%S")
        return df

    except Exception as e:
        print(f"❌ Yahoo API エラー {symbol}: {e}")
        return pd.DataFrame()


# ============================================================
# 1分足 → N分足
# ============================================================
def resample_nmin(df_1m: pd.DataFrame, n: int) -> pd.DataFrame:
    if df_1m.empty:
        return pd.DataFrame()

    df = df_1m.copy()
    df["time_range"] = pd.to_datetime(df["time_range"])
    df = df.sort_values("time_range").set_index("time_range")

    agg = {
        "open_price": "first",
        "high_price": "max",
        "low_price": "min",
        "close_price": "last",
        "volume": "sum",
    }

    df_n = df.resample(f"{n}min", origin="start_day").agg(agg).dropna()
    df_n = df_n.reset_index()
    df_n["time_range"] = df_n["time_range"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return df_n


# ============================================================
# 直近営業日の 5分足を読み込み（MA75補完用）
# ============================================================
def load_prev_5min(symbol: str, base_dir: str, limit=75):
    trading_day = get_latest_trading_day()
    date_str = trading_day.strftime("%Y%m%d")

    db_path = os.path.join(base_dir, f"summary{date_str}.db")
    if not os.path.exists(db_path):
        return pd.DataFrame()

    conn = sqlite3.connect(db_path)
    query = """
        SELECT time_range, close_price
        FROM stock_summary_5min
        WHERE symbol = ?
        ORDER BY time_range DESC
        LIMIT ?
    """
    df = pd.read_sql(query, conn, params=(symbol, limit))
    conn.close()

    return df.sort_values("time_range")


def add_ma75_with_prev(df_today_5m, df_prev_5m):
    if df_today_5m.empty:
        return df_today_5m

    df = pd.concat([df_prev_5m, df_today_5m], ignore_index=True)
    df["ma75"] = df["close_price"].rolling(75).mean()
    return df.iloc[len(df_prev_5m):].reset_index(drop=True)


# ============================================================
# DB保存
# ============================================================
def save_daily_by_interval(df, code, name, base_dir, interval):
    table = "stock_summary" if interval == 1 else f"stock_summary_{interval}min"

    df = df.copy()
    df["symbol"] = code
    df["symbolname"] = name
    df["date"] = pd.to_datetime(df["time_range"]).dt.strftime("%Y-%m-%d")
    df["last_update"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for d in df["date"].unique():
        daily = df[df["date"] == d]
        if daily.empty:
            continue

        db_path = os.path.join(base_dir, f"summary{d.replace('-', '')}.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        conn = sqlite3.connect(db_path)
        create_stock_summary_table_if_not_exists(conn, table)
        upgrade_stock_summary_table(conn, table)

        conn.execute(
            f"DELETE FROM {table} WHERE symbol=? AND date=?",
            (code, d)
        )

        daily.to_sql(table, conn, if_exists="append", index=False)
        conn.close()

        print(f"✔ {interval}分足 保存完了 {code} → {os.path.basename(db_path)}")


# ============================================================
# メイン処理
# ============================================================
def process_all(excel_path, output_dir):
    df_excel = pd.read_excel(excel_path)
    df_excel = df_excel[df_excel["市場・商品区分"].isin(
        ["プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"]
    )]

    for _, row in df_excel.iterrows():
        code = str(row["コード"]).zfill(4)
        name = row["銘柄名"]
        yahoo_symbol = f"{code}.T"

        print(f"\n🚀 {name}({code}) 開始")

        # 1分足
        df_1m = get_1min_data(yahoo_symbol)
        if df_1m.empty:
            print("⚠ データなし")
            continue

        df_1m = calc_indicators(df_1m)
        save_daily_by_interval(df_1m, code, name, output_dir, 1)

        # 3分足
        df_3m = resample_nmin(df_1m, 3)
        if not df_3m.empty:
            df_3m = calc_indicators(df_3m)
            save_daily_by_interval(df_3m, code, name, output_dir, 3)

        # 5分足（MA75補完あり）
        df_5m = resample_nmin(df_1m, 5)
        if not df_5m.empty:
            prev_5m = load_prev_5min(code, output_dir)
            df_5m = add_ma75_with_prev(df_5m, prev_5m)
            df_5m = calc_indicators(df_5m)
            save_daily_by_interval(df_5m, code, name, output_dir, 5)

    print("\n🎉 全銘柄 完了")
    convert_all()


# ============================================================
# 実行
# ============================================================
if __name__ == "__main__":
    os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
    process_all(EXCEL_FILE_PATH, OUTPUT_BASE_DIR)
