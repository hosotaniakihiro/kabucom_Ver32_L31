# ============================================================
# indicators.py（Ver23-FINAL-LTS 統合版）
# ------------------------------------------------------------
# Ver23 heavy（MA/EMA/MACD/RSI/RCI/BB/ATR/Volume Surge）
# ＋ Ver16 heavy（atr_ratio/gap/vol_ratio/dir_up/down）
#
# summary_controller（Ver23-LTS）と完全連動
# MultiDay df_merge 全体適用専用
# ============================================================

import pandas as pd
import numpy as np


# ============================================================
# 基本ユーティリティ
# ============================================================
def _sf(x, default=0.0):
    try:
        fx = float(x)
        if fx != fx:  # NaN
            return default
        return fx
    except:
        return default


# ============================================================
# ★ RSI
# ============================================================
def _calc_rsi(series: pd.Series, period: int = 14):
    diff = series.diff()
    up = diff.where(diff > 0, 0)
    dn = -diff.where(diff < 0, 0)

    ma_up = up.rolling(period).mean()
    ma_dn = dn.rolling(period).mean()

    return 100 * ma_up / (ma_up + ma_dn)


# ============================================================
# ★ RCI
# ============================================================
def _calc_rci(series: pd.Series, period: int = 9):
    rci = [np.nan] * len(series)
    arr = series.values

    for i in range(period - 1, len(arr)):
        window = arr[i - period + 1:i + 1]
        ranks = pd.Series(window).rank().values
        days = np.arange(period, 0, -1)
        diff = days - ranks
        sq = (diff * diff).sum()
        rci[i] = (1 - (6 * sq) / (period * (period*period - 1))) * 100

    return pd.Series(rci, index=series.index)


# ============================================================
# ★ BB 全セット（±1σ ±2σ ±3σ）
# ============================================================
def _calc_bb(series: pd.Series, period: int = 20):
    ma = series.rolling(period).mean()
    sd = series.rolling(period).std()

    up1 = ma + sd
    lo1 = ma - sd
    up2 = ma + sd * 2
    lo2 = ma - sd * 2
    up3 = ma + sd * 3
    lo3 = ma - sd * 3

    return ma, up1, lo1, up2, lo2, up3, lo3


# ============================================================
# ★ ATR 14（Ver23 + Ver16 両対応）
# ============================================================
def _calc_atr(df: pd.DataFrame, period: int = 14):
    high = df["high_price"].astype(float)
    low = df["low_price"].astype(float)
    close = df["close_price"].astype(float)

    prev_close = close.shift(1)

    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean()
    return atr


# ============================================================
# ★ 出来高（MA20, ratio）
# ============================================================
def _calc_volume_features(df: pd.DataFrame):
    vol = df["volume"].astype(float)
    vol_ma20 = vol.rolling(20, min_periods=1).mean()
    vol_ratio = np.where(vol_ma20 > 0, vol / vol_ma20, 0)
    return vol_ma20, vol_ratio


# ============================================================
# ★ gap_up / gap_down（1%以上）
# ============================================================
def _calc_gap(df: pd.DataFrame):
    o = df["open_price"].astype(float)
    c_prev = df["close_price"].astype(float).shift(1)

    gap_up = (o > c_prev * 1.01)
    gap_down = (o < c_prev * 0.99)

    return gap_up, gap_down


# ============================================================
# ★ 方向性フィルタ（Ver16）
# ============================================================
def _calc_direction(df: pd.DataFrame):
    close = df["close_price"].astype(float)
    ma5 = df["ma5"].astype(float)
    ma25 = df["ma25"].astype(float)

    slope = ma5.diff(3)

    # 🔼 上方向（ini と一致）
    ma_up = (close > ma5) & (ma5 > ma25) & (slope > 0)

    # 🔽 下方向（ini と一致）
    dir_down = (close < ma5) & (ma5 < ma25) & (slope < 0)

    return ma_up, dir_down


# ============================================================
# ★ heavy 指標（Ver23+Ver16｜Ultimate）
# ============================================================
def add_heavy_indicators(df: pd.DataFrame, interval: int = 1):

    if df is None or df.empty:
        return df

    df = df.copy()

    out = []

    # --------------------------------------------------------
    # シンボル単位で処理
    # --------------------------------------------------------
    for symbol, g in df.groupby("symbol"):

        g = g.sort_values(["date", "time"])

        c = g["close_price"].astype(float)

        # --- MA ---
        g["ma5"] = c.rolling(5).mean()
        g["ma25"] = c.rolling(25).mean()
        g["ma75"] = c.rolling(75).mean()

        # --- EMA ---
        g["ema12"] = c.ewm(span=12, adjust=False).mean()
        g["ema26"] = c.ewm(span=26, adjust=False).mean()

        # --- MACD ---
        g["macd"] = g["ema12"] - g["ema26"]
        g["signal"] = g["macd"].ewm(span=9, adjust=False).mean()
        g["hist"] = g["macd"] - g["signal"]

        # --- RSI ---
        g["rsi"] = _calc_rsi(c)

        # --- RCI ---
        g["rci"] = _calc_rci(c, period=9)

        # --- BB（±1,2,3σ） ---
        ma20, up1, lo1, up2, lo2, up3, lo3 = _calc_bb(c)
        g["bb_mid"] = ma20
        g["bb_upper"] = up1
        g["bb_lower"] = lo1
        g["bb_upper2"] = up2
        g["bb_lower2"] = lo2
        g["bb_upper3"] = up3
        g["bb_lower3"] = lo3

        # --- ATR ---
        g["atr"] = _calc_atr(g, 14)

        # --- ATR MA20 / ratio ---
        g["atr_ma20"] = g["atr"].rolling(20).mean()
        g["atr_ratio"] = np.where(g["atr_ma20"] > 0, g["atr"] / g["atr_ma20"], 0)
        g["atr_pct"] = np.where(c > 0, g["atr"] / c, 0)

        # --- volume MA20 / ratio ---
        g["vol_ma20"], g["vol_ratio"] = _calc_volume_features(g)

        # --- volume surge（Ver23）---
        g["vol_surge"] = g["volume"] / g["volume"].rolling(20).mean()

        # --- volume 前バー比 ---
        prev_vol = g["volume"].shift(1)
        g["vol_chg"] = (g["volume"] - prev_vol) / prev_vol.replace(0, np.nan)
        g["vol_chg"] = g["vol_chg"].fillna(0)

        # --- gap_up/down ---
        g["gap_up"], g["gap_down"] = _calc_gap(g)
        g["gap_pct"] = (g["open_price"] - g["close_price"].shift(1)) / \
                        g["close_price"].shift(1).replace(0, np.nan)

        # --- 方向性 ---
        g["dir_up"], g["dir_down"] = _calc_direction(g)

        out.append(g)

    return pd.concat(out, ignore_index=True)
