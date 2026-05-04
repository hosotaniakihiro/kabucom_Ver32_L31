# ============================================================
# indicator_light.py（Ver23-FINAL-LTS）
# ------------------------------------------------------------
# 1分足用：軽量テクニカル指標
# MA5 / MA25 / MA75 / RSI / RCI / BB
# ============================================================

import pandas as pd
import numpy as np

def _calc_rci(series: pd.Series, window: int = 9):
    n = window
    if len(series) < n:
        return pd.Series([np.nan] * len(series), index=series.index)

    result = []
    for i in range(len(series)):
        if i < n - 1:
            result.append(np.nan)
            continue
        window_vals = series.iloc[i-n+1:i+1]
        ranks_price = window_vals.rank()
        ranks_time = pd.Series(range(1, n+1), index=window_vals.index)
        d = ranks_time - ranks_price
        rci_val = (1 - 6 * (d**2).sum() / (n*(n**2 - 1))) * 100
        result.append(rci_val)
    return pd.Series(result, index=series.index)

def add_light_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # 移動平均
    df["ma5"] = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(5).mean())
    df["ma25"] = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(25).mean())
    df["ma75"] = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(75).mean())

    # RSI（14）
    def _rsi(x):
        delta = x.diff()
        up = delta.clip(lower=0)
        down = -delta.clip(upper=0)
        roll_up = up.rolling(14).mean()
        roll_down = down.rolling(14).mean()
        rs = roll_up / roll_down
        return 100 - (100 / (1 + rs))

    df["rsi"] = df.groupby("symbol")["close_price"].transform(_rsi)

    # RCI
    df["rci"] = df.groupby("symbol")["close_price"].transform(lambda x: _calc_rci(x, 9))

    # ボリンジャーバンド（20）
    def _bb(x):
        ma = x.rolling(20).mean()
        std = x.rolling(20).std()
        return ma + 2*std, ma - 2*std

    bb_up, bb_low = _bb(df.groupby("symbol")["close_price"].transform(lambda x: x))
    df["bb_upper"] = bb_up
    df["bb_lower"] = bb_low

    return df
