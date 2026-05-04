# ============================================================
# lazy_indicators.py
# ------------------------------------------------------------
# ✔ 必要な indicator だけ計算
# ✔ startup を爆速化
# ============================================================

import pandas as pd
import logging

from indicators import (
    calc_ma,
    calc_rsi,
    calc_macd,
    calc_atr,
    calc_bollinger,
)

logger = logging.getLogger(__name__)

def ensure_indicators(
    df: pd.DataFrame,
    indicators: list[str],
) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    for ind in indicators:
        if ind == "ma75" and "ma75" not in df.columns:
            df["ma75"] = calc_ma(df, period=75)

        elif ind == "ma25" and "ma25" not in df.columns:
            df["ma25"] = calc_ma(df, period=25)

        elif ind == "ma5" and "ma5" not in df.columns:
            df["ma5"] = calc_ma(df, period=5)

        elif ind == "rsi" and "rsi" not in df.columns:
            df["rsi"] = calc_rsi(df)

        elif ind == "macd" and "macd" not in df.columns:
            macd, sig, hist = calc_macd(df)
            df["macd"] = macd
            df["macd_signal"] = sig
            df["macd_hist"] = hist

        elif ind == "atr" and "atr" not in df.columns:
            df["atr"] = calc_atr(df)

        elif ind == "bb" and "bb_upper" not in df.columns:
            up, mid, low = calc_bollinger(df)
            df["bb_upper"] = up
            df["bb_middle"] = mid
            df["bb_lower"] = low

    return df
