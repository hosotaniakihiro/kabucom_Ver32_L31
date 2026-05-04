# ============================================================
# heavy_calculator.py（Ver24-FINAL-GROUPED-REV2）
# ------------------------------------------------------------
# ✔ symbol ごとに完全分離計算（最重要）
# ✔ 既存 SAFE ロジック完全維持
# ✔ min_periods 明示で NaN 地獄を完全抑制
# ✔ 起動直後 / 寄り直後 / 日跨ぎでも安定
# ============================================================

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# 安全 Series 化
# ------------------------------------------------------------
def safe_series(df, col, default=0):
    """
    df[col] が存在しない / scalar の場合でも
    index 揃いの Series を必ず返す
    """
    if col in df.columns:
        s = df[col]
        if isinstance(s, pd.Series):
            return s
        return pd.Series([s] * len(df), index=df.index)
    return pd.Series([default] * len(df), index=df.index)


# ------------------------------------------------------------
# heavy 指標計算（★ symbol ごとに groupby）
# ------------------------------------------------------------
def recalc_heavy_indicators(df: pd.DataFrame, interval: int):
    """
    interval:
        1 / 3 / 5（将来拡張用、現状はロジック共通）
    """

    if df is None or df.empty:
        return df

    out = []

    # ★ 最重要：symbol 完全分離
    for symbol, g in df.groupby("symbol"):
        g = g.sort_values("datetime").copy()

        # データ不足でも落とさない
        if len(g) < 3:
            out.append(g)
            continue

        # ====================================================
        # 基本 OHLCV（完全安全化）
        # ====================================================
        close = pd.to_numeric(
            safe_series(g, "close_price"),
            errors="coerce"
        ).fillna(0)

        high = pd.to_numeric(
            safe_series(g, "high_price"),
            errors="coerce"
        ).fillna(close)

        low = pd.to_numeric(
            safe_series(g, "low_price"),
            errors="coerce"
        ).fillna(close)

        open_ = pd.to_numeric(
            safe_series(g, "open_price"),
            errors="coerce"
        ).fillna(close)

        vol = pd.to_numeric(
            safe_series(g, "volume"),
            errors="coerce"
        ).fillna(0)

        # ====================================================
        # VWAP（累積・ゼロ除算防止）
        # ====================================================
        g["turnover"] = vol * close
        cum_turn = g["turnover"].cumsum()
        cum_vol = vol.replace(0, np.nan).cumsum()
        g["vwap"] = (cum_turn / cum_vol).fillna(close)

        # ====================================================
        # MA（min_periods 明示）
        # ====================================================
        g["ma5"]  = close.rolling(5,  min_periods=1).mean()
        g["ma25"] = close.rolling(25, min_periods=1).mean()
        g["ma75"] = close.rolling(75, min_periods=1).mean()

        # ====================================================
        # EMA / MACD（NaN 連鎖防止）
        # ====================================================
        g["ema12"] = close.ewm(span=12, adjust=False, min_periods=1).mean()
        g["ema26"] = close.ewm(span=26, adjust=False, min_periods=1).mean()
        g["macd"]  = g["ema12"] - g["ema26"]
        g["signal"] = g["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
        g["hist"]   = g["macd"] - g["signal"]

        # ====================================================
        # RSI（TA-Lib 優先、fallback 安全）
        # ====================================================
        try:
            import talib
            g["rsi"] = talib.RSI(close, timeperiod=14)
        except Exception:
            diff = close.diff()
            up = diff.clip(lower=0)
            down = (-diff).clip(lower=0)
            ma_up = up.rolling(14, min_periods=1).mean()
            ma_down = down.rolling(14, min_periods=1).mean()
            rs = ma_up / ma_down.replace(0, np.nan)
            g["rsi"] = 100 - 100 / (1 + rs)

        # ====================================================
        # RCI（安全実装）
        # ====================================================
        n = 9
        rci_vals = []
        for i in range(len(g)):
            if i < n:
                rci_vals.append(np.nan)
                continue
            w = close.iloc[i - n + 1:i + 1]
            rp = w.rank().to_numpy()
            rt = np.arange(1, n + 1)
            d = rt - rp
            rci_vals.append(
                (1 - (6 * np.sum(d ** 2)) / (n * (n ** 2 - 1))) * 100
            )
        g["rci"] = rci_vals

        # ====================================================
        # Bollinger Bands（min_periods 明示）
        # ====================================================
        mid = close.rolling(20, min_periods=1).mean()
        width = close.rolling(20, min_periods=1).std()

        g["bb_mid"] = mid
        g["bb_upper"]  = mid + width
        g["bb_lower"]  = mid - width
        g["bb_upper2"] = mid + width * 2
        g["bb_lower2"] = mid - width * 2
        g["bb_upper3"] = mid + width * 3
        g["bb_lower3"] = mid - width * 3
        g["bb_width"]  = width

        # ====================================================
        # ATR（完全安全）
        # ====================================================
        tr = pd.Series(
            np.maximum.reduce([
                (high - low).to_numpy(),
                (high - close.shift()).abs().to_numpy(),
                (low - close.shift()).abs().to_numpy(),
            ]),
            index=g.index
        )

        g["tr"] = tr
        g["atr"] = tr.rolling(14, min_periods=1).mean()
        g["atr_ma20"] = g["atr"].rolling(20, min_periods=1).mean()
        g["atr_ratio"] = g["atr"] / close.replace(0, np.nan)
        g["atr_pct"] = g["atr_ratio"] * 100

        # ====================================================
        # Volume 系
        # ====================================================
        g["vol_ma20"] = vol.rolling(20, min_periods=1).mean()
        g["vol_ratio"] = vol / g["vol_ma20"].replace(0, np.nan)
        g["vol_surge"] = g["vol_ratio"]
        g["vol_chg"] = vol.pct_change()

        # ====================================================
        # GAP
        # ====================================================
        prev = close.shift()
        g["gap_up"] = open_ - prev
        g["gap_down"] = prev - open_
        g["gap_pct"] = g["gap_up"] / prev.replace(0, np.nan) * 100

        # ====================================================
        # Direction
        # ====================================================
        g["dir_up"] = (close > open_).astype(int)
        g["dir_down"] = (close < open_).astype(int)

        out.append(g)

    return pd.concat(out, ignore_index=True)
