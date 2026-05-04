# ============================================================
# PUSH ratio calculator
# ------------------------------------------------------------
# ✔ 直近N本の close のうち PUSH 由来の割合
# ✔ 表示・AI特徴量用
# ============================================================

import pandas as pd

def calc_push_ratio(
    df_close: pd.DataFrame,
    symbol: str,
    lookback: int = 25,
) -> float:
    """
    PUSH率（0.0〜1.0）
    """
    if df_close is None or df_close.empty:
        return 0.0

    df = df_close[df_close["symbol"] == symbol].tail(lookback)
    if df.empty:
        return 0.0

    push_count = (df["source"] == "PUSH").sum()
    return push_count / len(df)
