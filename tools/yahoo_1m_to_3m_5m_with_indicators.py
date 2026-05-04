# ============================================================
# yahoo_1m_to_3m_5m_with_indicators.py
# ------------------------------------------------------------
# ・Yahoo 1分足を正本として使用
# ・1m / 3m / 5m の OHLCV 作成
# ・各足でテクニカル指標を計算
# ============================================================

import pandas as pd
import numpy as np
import datetime as dt
import yfinance as yf


# ============================================================
# 設定
# ============================================================
MA_LIST = [5, 25, 75]
RSI_PERIOD = 14
ATR_PERIOD = 14
BB_PERIOD = 20


# ============================================================
# tz-aware → tz-naive
# ============================================================
def to_naive(s):
    try:
        return pd.to_datetime(s, utc=True).dt.tz_convert(None)
    except Exception:
        return pd.to_datetime(s, errors="coerce")


# ============================================================
# Yahoo 1分足取得
# ============================================================
def fetch_yahoo_1min(symbol: str, days=3) -> pd.DataFrame:
    ticker = yf.Ticker(f"{symbol}.T")
    df = ticker.history(interval="1m", period=f"{days}d")

    if df.empty:
        return pd.DataFrame()

    df = df.reset_index()

    df = df.rename(columns={
        "Datetime": "datetime",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    })

    df["datetime"] = to_naive(df["datetime"])
    df = df.dropna(subset=["datetime"])

    df = df.sort_values("datetime").reset_index(drop=True)

    return df


# ============================================================
# テクニカル指標
# ============================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    # --- MA ---
    for ma in MA_LIST:
        df[f"ma{ma}"] = df["close"].rolling(ma).mean()

    # --- RSI ---
    diff = df["close"].diff()
    gain = diff.clip(lower=0)
    loss = -diff.clip(upper=0)
    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    # --- ATR ---
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)

    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    # --- Bollinger Band ---
    ma = df["close"].rolling(BB_PERIOD).mean()
    std = df["close"].rolling(BB_PERIOD).std()
    df["bb_upper"] = ma + 2 * std
    df["bb_lower"] = ma - 2 * std

    return df


# ============================================================
# リサンプリング（1m → 3m / 5m）
# ============================================================
def resample_tf(df_1m: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    if df_1m.empty:
        return df_1m

    df = df_1m.copy()
    df = df.set_index("datetime")

    df_tf = df.resample(f"{tf_min}T").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })

    df_tf = df_tf.dropna(subset=["open"])
    df_tf = df_tf.reset_index()

    return df_tf


# ============================================================
# 実行例
# ============================================================
if __name__ == "__main__":

    symbol = "7203"  # トヨタ
    days = 3         # MA75 安定用

    print(f"📥 Yahoo 1m download: {symbol}")

    # --- 1分足 ---
    df_1m = fetch_yahoo_1min(symbol, days)
    df_1m = add_indicators(df_1m)

    # --- 3分足 ---
    df_3m = resample_tf(df_1m, 3)
    df_3m = add_indicators(df_3m)

    # --- 5分足 ---
    df_5m = resample_tf(df_1m, 5)
    df_5m = add_indicators(df_5m)

    print("\n=== 1min sample ===")
    print(df_1m.tail(3)[["datetime", "close", "ma5", "ma25", "ma75", "rsi"]])

    print("\n=== 3min sample ===")
    print(df_3m.tail(3)[["datetime", "close", "ma25", "ma75", "rsi"]])

    print("\n=== 5min sample ===")
    print(df_5m.tail(3)[["datetime", "close", "ma25", "ma75", "rsi"]])
