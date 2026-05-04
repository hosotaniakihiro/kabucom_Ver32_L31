# ============================================================
# trading/indicators/add_indicators.py
# (Ver26-FINAL-INDICATOR-INJECTOR)
# ------------------------------------------------------------
# ✔ MA / RSI / MACD の唯一の計算場所
# ✔ calculator 出力DFを破壊しない
# ✔ symbol 単位・時系列保証
# ✔ indicator_ready 判定の前提を作る
# ============================================================

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# 移動平均
# ------------------------------------------------------------
def _add_ma(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    for p in periods:
        col = f"ma{p}"
        df[col] = (
            df.groupby("symbol")["close_price"]
            .transform(lambda x: x.rolling(p, min_periods=p).mean())
        )
    return df


# ------------------------------------------------------------
# RSI
# ------------------------------------------------------------
def _add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    def _calc(x):
        diff = x.diff()
        gain = diff.clip(lower=0)
        loss = -diff.clip(upper=0)

        avg_gain = gain.rolling(period, min_periods=period).mean()
        avg_loss = loss.rolling(period, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    df["rsi"] = (
        df.groupby("symbol")["close_price"]
        .transform(_calc)
    )
    return df


# ------------------------------------------------------------
# MACD
# ------------------------------------------------------------
def _add_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:

    def _calc(x):
        ema_fast = x.ewm(span=fast, adjust=False).mean()
        ema_slow = x.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal_line = macd.ewm(span=signal, adjust=False).mean()
        hist = macd - signal_line
        return macd, signal_line, hist

    macd, signal_line, hist = zip(
        *df.groupby("symbol")["close_price"].apply(_calc)
    )

    df["macd"] = np.concatenate(macd)
    df["macd_signal"] = np.concatenate(signal_line)
    df["macd_hist"] = np.concatenate(hist)

    return df


# ------------------------------------------------------------
# 🔥 メイン：指標一括注入
# ------------------------------------------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    calculator 直後で呼び出す
    """

    if df is None or df.empty:
        return df

    # 並び順保証
    df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    # --- MA ---
    df = _add_ma(df, periods=[5, 25, 75])

    # --- RSI ---
    df = _add_rsi(df, period=14)

    # --- MACD ---
    df = _add_macd(df)

    logger.info(
        f"[INDICATOR] injected MA/RSI/MACD rows={len(df)}"
    )

    return df
