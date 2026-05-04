import pandas as pd
import numpy as np
from daily_loader import load_daily_last_12m


# ============================
# ★ 標準的な RSI（Wilder's RSI）
# ============================
def compute_rsi(series, period=14):
    delta = series.diff()

    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)

    roll_up = up.rolling(period).mean()
    roll_down = down.rolling(period).mean()

    rs = roll_up / (roll_down + 1e-9)
    rsi = 100 - (100 / (1 + rs))

    return rsi


# ============================
# ★ 日足特徴量（最新 MTF-AI 用）
# ============================
def build_daily_features():

    df = load_daily_last_12m()

    # ---- MA ----
    df["day_ma5"]  = df.groupby("symbol")["close"].transform(lambda x: x.rolling(5).mean())
    df["day_ma25"] = df.groupby("symbol")["close"].transform(lambda x: x.rolling(25).mean())
    df["day_ma75"] = df.groupby("symbol")["close"].transform(lambda x: x.rolling(75).mean())

    # ---- RSI ----
    df["day_rsi"] = df.groupby("symbol")["close"].transform(compute_rsi)

    # ---- ローソク足位置 ----
    df["day_range"] = df["high"] - df["low"]
    df["day_pos"] = (df["close"] - df["low"]) / (df["day_range"] + 1e-9)

    # ---- 当日のボラティリティ ----
    df["day_volatility"] = df["day_range"] / (df["close"] + 1e-9)

    # ---- 出来高変化 ----
    df["day_vol_ma5"] = df.groupby("symbol")["volume"].transform(lambda x: x.rolling(5).mean())
    df["vol_ratio"] = df["volume"] / (df["day_vol_ma5"] + 1e-9)

    # ---- 当日の上昇率 ----
    df["day_change"] = df.groupby("symbol")["close"].pct_change()

    # ---- 長期トレンド方向 ----
    df["day_trend_flag"] = np.where(df["day_ma25"] > df["day_ma75"], 1,
                              np.where(df["day_ma25"] < df["day_ma75"], -1, 0))

    # ---- 欠損除外 ----
    df = df.dropna(subset=["day_ma25", "day_ma75"])

    return df
