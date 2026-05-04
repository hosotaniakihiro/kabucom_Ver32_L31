# ============================================================
# summary_indicator_recalc.py（Ver H – MultiDay完全対応）
# ------------------------------------------------------------
# merged summary（1m / 3m / 5m）に対して
# - 軽量インジケータ   → PUSH更新時
# - heavyインジケータ → Yahoo補完 / 起動時再生成
#
# すべての価格カラム（open/open_price, close/close_price 等）
# に完全対応する安定・高速な指標モジュール。
# ============================================================

import logging
import numpy as np
import pandas as pd

from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD
from ta.volatility import BollingerBands

from global_state import global_data

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# カラム名吸収（Yahoo / PUSH / summaryDB）
# ------------------------------------------------------------
def _unify_columns(df):
    df = df.copy()

    rename_map = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
        "price": "close_price",
        "currentprice": "close_price",
    }

    df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns},
              inplace=True)

    # 欠損カラムは NaN で作成
    for c in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        if c not in df.columns:
            df[c] = np.nan

    return df


# ============================================================
# 軽量インジケータ（MA5 / MA25 / MA75 / VWAP / MACD / RSI）
# ============================================================
def _calc_light(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = _unify_columns(df)
    df = df.sort_values(["symbol", "datetime"]).copy()

    # ---- MA ----
    df["ma5"] = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(5).mean())
    df["ma25"] = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(25).mean())
    df["ma75"] = df.groupby("symbol")["close_price"].transform(lambda x: x.rolling(75).mean())

    # ---- VWAP（累積方式）----
    try:
        df["pv"] = df["close_price"] * df["volume"]
        df["cum_pv"] = df.groupby("symbol")["pv"].cumsum()
        df["cum_vol"] = df.groupby("symbol")["volume"].cumsum()
        df["vwap"] = df["cum_pv"] / df["cum_vol"]
    except Exception as e:
        logger.warning(f"VWAP計算エラー: {e}")
        df["vwap"] = np.nan

    # ---- MACD ----
    try:
        macd = MACD(df["close_price"])
        df["macd"] = macd.macd()
        df["signal"] = macd.macd_signal()
    except Exception as e:
        logger.warning(f"MACD計算エラー: {e}")
        df["macd"] = df["signal"] = np.nan

    # ---- RSI ----
    try:
        df["rsi"] = RSIIndicator(df["close_price"], window=14).rsi()
    except Exception as e:
        logger.warning(f"RSI計算エラー: {e}")
        df["rsi"] = np.nan

    # 不要列削除
    df.drop(columns=[c for c in ["pv", "cum_pv", "cum_vol"] if c in df.columns],
            inplace=True)

    return df


# ============================================================
# heavyインジケータ（RCI / Stoch / Bollinger / ATR）
# ============================================================
def _calc_heavy(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = _unify_columns(df)
    df = df.sort_values(["symbol", "datetime"]).copy()

    # --------------------------------------------------------
    # RCI
    # --------------------------------------------------------
    def rci(series, period=9):
        if len(series) < period:
            return pd.Series([np.nan] * len(series))

        values = series.values
        result = []

        for i in range(len(series)):
            if i < period - 1:
                result.append(np.nan)
                continue

            window = pd.Series(values[i - period + 1:i + 1])
            ranks = window.rank().values
            t = np.arange(1, period + 1)
            d = np.sum((ranks - t) ** 2)
            rci_val = (1 - (6 * d) / (period * (period ** 2 - 1))) * 100
            result.append(rci_val)

        return pd.Series(result, index=series.index)

    df["rci"] = df.groupby("symbol")["close_price"].transform(rci)

    # --------------------------------------------------------
    # Stochastic
    # --------------------------------------------------------
    try:
        so = StochasticOscillator(
            high=df["high_price"],
            low=df["low_price"],
            close=df["close_price"],
            window=14,
            smooth_window=3,
        )
        df["slowk"] = so.stoch()
        df["slowd"] = so.stoch_signal()
    except Exception as e:
        logger.warning(f"Stoch計算エラー: {e}")
        df["slowk"] = df["slowd"] = np.nan

    # --------------------------------------------------------
    # Bollinger Bands
    # --------------------------------------------------------
    try:
        bb2 = BollingerBands(df["close_price"], window=20, window_dev=2)
        df["bb_upper"] = bb2.bollinger_hband()
        df["bb_lower"] = bb2.bollinger_lband()

        bb3 = BollingerBands(df["close_price"], window=20, window_dev=3)
        df["bb_upper_3"] = bb3.bollinger_hband()
        df["bb_lower_3"] = bb3.bollinger_lband()
    except Exception as e:
        logger.warning(f"Bollinger計算エラー: {e}")
        df["bb_upper"] = df["bb_lower"] = np.nan
        df["bb_upper_3"] = df["bb_lower_3"] = np.nan

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------
    try:
        high = df["high_price"]
        low = df["low_price"]
        close = df["close_price"]
        prev_close = close.shift(1)

        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)

        df["atr"] = tr.rolling(14).mean()

    except Exception as e:
        logger.warning(f"ATR計算エラー: {e}")
        df["atr"] = np.nan

    return df


# ============================================================
# 外部向けAPI：軽量
# ============================================================
def recalc_light_indicators(interval: int):
    df = global_data.get_merged_summary(interval)
    if df is None or df.empty:
        return
    df2 = _calc_light(df)
    global_data.set_merged_summary(interval, df2)


# ============================================================
# 外部向けAPI：heavy（全 interval 対象）
# ============================================================
def recalc_all_indicators():
    for interval in (1, 3, 5):
        df = global_data.get_merged_summary(interval)
        if df is None or df.empty:
            continue

        df1 = _calc_light(df)
        df2 = _calc_heavy(df1)
        global_data.set_merged_summary(interval, df2)

    logger.info("🟢 heavy指標 計算完了（1m/3m/5m）")
