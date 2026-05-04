# ============================================================
# File   : trading/summary/summary_full_calculator.py
# Created: 2025-12-22 JST
# ------------------------------------------------------------
# ✔ BULK / FULL summary calculator
# ✔ calculate_summary は「位置引数のみ」対応
# ============================================================

import pandas as pd
from trading.summary.calculator import calculate_summary


def calculate_summary_full(
    df: pd.DataFrame,
    symbols: list[str],
) -> pd.DataFrame:
    """
    BULK rebuild 用 FULL summary 計算
    """

    # ★ 位置引数で渡す（ここが超重要）
    return calculate_summary(
        df,                    # df_1m
        pd.DataFrame(),         # df_summary（空）
        symbols,                # symbols
        None,                   # start_time
        None,                   # end_time
    )
