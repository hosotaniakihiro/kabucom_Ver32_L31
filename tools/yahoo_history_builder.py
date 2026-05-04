# ============================================================
# tools/yahoo_summary_builder.py（59日前から日単位DB保存・安定版）
# ============================================================

import os
import pandas as pd
import numpy as np
import datetime as dt
import yfinance as yf
import logging
import sqlite3
import configparser
import warnings
import time

from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD
from ta.volatility import BollingerBands

# --- 警告無効化 ---
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)

# ============================================================
# 設定とログ
# ============================================================
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
BASE_PATH = conf.get("paths", "base_path", fallback="y:/stock_price_data/")
os.makedirs(BASE_PATH, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============================================================
# Excel から銘柄リストを読み込み
# ============================================================
def load_target_symbols(excel_path="y:/kabu/data_j.xls") -> pd.DataFrame:
    try:
        df = pd.read_excel(excel_path, engine="xlrd", header=0)
        codes = df.iloc[:, 1]  # B列（コード）
        markets = df.iloc[:, 3]  # D列（市場）

        df_filtered = pd.DataFrame({"symbol": codes, "market": markets})
        df_filtered = df_filtered[df_filtered["market"].isin(
            ["プライム（内国株式）", "グロース（内国株式）", "スタンダード（内国株式）"]
        )]
        df_filtered["symbol"] = df_filtered["symbol"].astype(str).str.strip()
        logger.info(f"✅ 対象銘柄数: {len(df_filtered)}件 ({os.path.basename(excel_path)})")
        return df_filtered.reset_index(drop=True)
    except Exception as e:
        logger.error(f"❌ Excel読み込み失敗: {e}")
        return pd.DataFrame(columns=["symbol", "market"])

# ============================================================
# 指標計算関数
# ============================================================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    try:
        df = df.copy()

        # --- 移動平均 & VWAP ---
        df["ma5"] = df["close_price"].rolling(5, min_periods=1).mean()
        df["ma25"] = df["close_price"].rolling(25, min_periods=1).mean()
        df["ma75"] = df["close_price"].rolling(75, min_periods=1).mean()
        df["vwap"] = (df["close_price"] * df["volume"]).cumsum() / df["volume"].cumsum()

        # --- MACD ---
        macd = MACD(df["close_price"], window_slow=26, window_fast=12, window_sign=9)
        df["macd"] = macd.macd()
        df["signal"] = macd.macd_signal()

        # --- RSI ---
        df["rsi"] = RSIIndicator(df["close_price"], window=14).rsi()

        # --- ストキャス ---
        stoch = StochasticOscillator(
            high=df["high_price"], low=df["low_price"], close=df["close_price"], window=14, smooth_window=3
        )
        df["slowk"] = stoch.stoch()
        df["slowd"] = stoch.stoch_signal()

        # --- ボリンジャーバンド ---
        bb2 = BollingerBands(df["close_price"], window=20, window_dev=2)
        df["bb_upper"], df["bb_lower"] = bb2.bollinger_hband(), bb2.bollinger_lband()
        bb3 = BollingerBands(df["close_price"], window=20, window_dev=3)
        df["bb_upper_3"], df["bb_lower_3"] = bb3.bollinger_hband(), bb3.bollinger_lband()

        # --- RCI ---
        df["rci"] = np.nan
        period = 9
        if len(df) >= period:
            ranks = np.arange(1, period + 1)
            for i in range(period - 1, len(df)):
                win = df["close_price"].iloc[i - period + 1:i + 1].reset_index(drop=True)
                pr = win.rank(method="first").to_numpy()
                d = ranks - pr
                df.at[df.index[i], "rci"] = (1 - (6 * np.sum(d**2)) / (period * (period**2 - 1))) * 100

        return df

    except Exception as e:
        logger.warning(f"⚠️ 指標計算エラー: {e}")
        return df

# ============================================================
# Yahooデータ取得（1日単位）
# ============================================================
def fetch_yahoo_data(symbol: str, target_day: dt.date, interval: int = 5) -> pd.DataFrame:
    try:
        yahoo_symbol = f"{symbol}.T"
        start = dt.datetime.combine(target_day, dt.time(0, 0))
        end = start + dt.timedelta(days=1)

        df = yf.download(
            yahoo_symbol,
            interval=f"{interval}m",
            start=start,
            end=end,
            auto_adjust=False,
            progress=False,
        )

        if df.empty:
            logger.warning(f"⚠️ {symbol}: データなし ({target_day})")
            return pd.DataFrame()

        # --- MultiIndex対応 ---
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        df = df.reset_index().rename(columns={
            "Datetime": "end_time",
            "Open": "open_price",
            "High": "high_price",
            "Low": "low_price",
            "Close": "close_price",
            "Volume": "volume",
        })

        df["symbol"] = symbol
        df["start_time"] = df["end_time"] - pd.Timedelta(minutes=interval)
        df["date"] = df["end_time"].dt.date
        df["time"] = df["end_time"].dt.time
        df["time_range"] = df["start_time"].dt.strftime("%H:%M") + " - " + df["end_time"].dt.strftime("%H:%M")

        return calculate_indicators(df)

    except Exception as e:
        logger.warning(f"⚠️ {symbol}: 取得エラー ({e})")
        return pd.DataFrame()

# ============================================================
# DB保存（日次 summaryYYYYMMDD.db）
# ============================================================
def save_daily_summary(df_all: pd.DataFrame, target_day: dt.date, interval: int = 5):
    if df_all.empty:
        logger.warning(f"⚠️ {target_day}: 保存対象データなし → スキップ")
        return

    db_path = os.path.join(BASE_PATH, f"summary{target_day.strftime('%Y%m%d')}.db")
    conn = sqlite3.connect(db_path)
    table = f"stock_summary_{interval}min"
    try:
        df_all.to_sql(table, conn, if_exists="replace", index=False)
        conn.commit()
        logger.info(f"💾 {target_day}: {len(df_all)}件保存完了 ({os.path.basename(db_path)})")
    except Exception as e:
        logger.error(f"❌ DB保存失敗 ({target_day}): {e}", exc_info=True)
    finally:
        conn.close()

# ============================================================
# メイン処理：59日前から日単位でDB生成
# ============================================================
def main():
    df_symbols = load_target_symbols()
    if df_symbols.empty:
        logger.error("❌ 銘柄リストが空 → 終了")
        return

    start_day = dt.date.today() - dt.timedelta(days=56)
    end_day = dt.date.today()

    logger.info(f"📅 対象期間: {start_day} ～ {end_day}")

    for d in pd.date_range(start_day, end_day):
        target_day = d.date()
        logger.info(f"🗓 {target_day}: データ収集開始")

        all_data = []
        for i, row in df_symbols.iterrows():
            symbol = str(row["symbol"]).strip()
            if not symbol:
                continue
            logger.info(f"({i+1}/{len(df_symbols)}) {symbol} 取得中...")
            df = fetch_yahoo_data(symbol, target_day, interval=5)
            if not df.empty:
                all_data.append(df)

        if not all_data:
            logger.warning(f"⚠️ {target_day}: すべて空 → スキップ")
            continue

        df_all = pd.concat(all_data, ignore_index=True)
        save_daily_summary(df_all, target_day, interval=5)

        logger.info(f"✅ {target_day}: 完了 ({len(df_all)}件)")
        time.sleep(1)  # API制限回避

    logger.info("🎉 全59日分のYahooデータ収集・保存完了")

# ============================================================
if __name__ == "__main__":
    main()
