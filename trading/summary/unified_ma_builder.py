# ============================================================
# trading/summary/unified_ma_builder.py
# ------------------------------------------------------------
# ✔ Unified close から MA を生成
# ✔ MA5 / MA25 / MA75
# ✔ Series 単位で rolling（object 列完全排除）
# ✔ DataError / TypeError 完全防止
# ============================================================

import pandas as pd
from typing import Tuple


def build_unified_ma_1min(
    df_close: pd.DataFrame,
    windows: Tuple[int, ...] = (5, 25, 75),
) -> pd.DataFrame:

    if df_close is None or df_close.empty:
        return pd.DataFrame()

    # 必須列
    required = {"symbol", "datetime", "close"}
    if not required.issubset(df_close.columns):
        return pd.DataFrame()

    df = df_close.copy()

    # close を数値に強制
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    # MA 計算できない行を除外
    df = df.dropna(subset=["close"])
    if df.empty:
        return pd.DataFrame()

    # 並び替え必須
    df = df.sort_values(["symbol", "datetime"])

    # 出力は必要最小限
    out = df[["symbol", "datetime"]].copy()

    # --------------------------------------------------------
    # MA 計算（Series に対してのみ）
    # --------------------------------------------------------
    for w in windows:
        out[f"ma{w}"] = (
            df.groupby("symbol", sort=False)["close"]
              .rolling(window=w, min_periods=w)
              .mean()
              .reset_index(level=0, drop=True)
        )

    return out
