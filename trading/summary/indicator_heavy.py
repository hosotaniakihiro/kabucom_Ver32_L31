# ============================================================
# indicator_heavy.py（Ver23-FINAL-LTS-REV4 完全安定版）
# ------------------------------------------------------------
# ・groupby.apply 完全廃止（duplicate index 100% 回避）
# ・ATR / BB / dir / slope すべて transform ベース
# ・df.reset_index(drop=True) で index 衝突を完全防止
# ------------------------------------------------------------

import pandas as pd
import numpy as np


# ============================================================
# ATR transform（apply 廃止）
# ============================================================
def _atr_transform_df(df: pd.DataFrame, period=14):
    """
    transform ベースで ATR を 1:1 で返す。
    df.index をそのまま維持し duplicate 無し。
    """

    high = df["high_price"]
    low = df["low_price"]
    close = df["close_price"]

    # 前の終値
    prev_close = close.groupby(df["symbol"]).shift(1)
    prev_close = prev_close.fillna(close)  # 最初は自身と同じ値に

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.groupby(df["symbol"]).transform(
        lambda x: x.rolling(period).mean()
    )
    return atr


# ============================================================
# 方向 transform
# ============================================================
def _direction_transform(close):
    diff = close.diff()
    return diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))


# ============================================================
# slope（rolling window 内でも動く安全版）
# ============================================================
def _safe_slope_array(arr):
    """
    arr: numpy array（NaN含む）
    """
    win = len(arr)
    if win < 2:
        return np.nan

    x = np.arange(win)
    try:
        slope = np.polyfit(x, arr, 1)[0]
        return slope
    except Exception:
        return np.nan


def _slope_transform(series, win=25):
    values = series.to_numpy()
    out = [np.nan] * len(values)

    for i in range(win - 1, len(values)):
        y = values[i - win + 1:i + 1]
        out[i] = _safe_slope_array(y)

    return pd.Series(out, index=series.index)


# ============================================================
# ★ heavy 指標 本体
# ============================================================
def add_heavy_indicators(df: pd.DataFrame, interval: int) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    # --------------------------------------------------------
    # duplicate index 撲滅
    # --------------------------------------------------------
    df = df.reset_index(drop=True).copy()

    # --------------------------------------------------------
    # 数値変換の安全性向上
    # --------------------------------------------------------
    for col in ("close_price", "high_price", "low_price", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # --------------------------------------------------------
    # ATR（完全 transform ベース）
    # --------------------------------------------------------
    df["atr"] = _atr_transform_df(df)

    # --------------------------------------------------------
    # ボリンジャーバンド幅（20期間）
    # --------------------------------------------------------
    ma20 = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(20).mean())
    std20 = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(20).std())
    df["bb_width"] = (2 * std20) / ma20

    # --------------------------------------------------------
    # volume change（%変化）
    # --------------------------------------------------------
    df["volume_change"] = df.groupby("symbol")["volume"].pct_change()

    # --------------------------------------------------------
    # direction（+1 / -1 / 0）
    # --------------------------------------------------------
    df["dir"] = df.groupby("symbol")["close_price"].transform(_direction_transform)

    # --------------------------------------------------------
    # slope（MA25 の傾き）
    # --------------------------------------------------------
    ma25 = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(25).mean())
    df["slope_ma25"] = df.groupby("symbol")[ "close_price" ].transform(
        lambda x: _slope_transform(x, win=25)
    )

    return df
