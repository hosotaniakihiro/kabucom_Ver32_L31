#evaluate_entry_conditions.py

import pandas as pd
def check_entry_conditions(df_symbol: pd.DataFrame, side: str) -> bool:
    """
    売買条件をチェックして True / False を返す
    - side: "BUY" or "SELL"
    """
    if df_symbol is None or len(df_symbol) < 3:
        return False

    last3 = df_symbol.tail(3).copy()

    # 直近のローソク
    prev2 = last3.iloc[0]  # 前の前
    prev1 = last3.iloc[1]  # 前
    latest = last3.iloc[2]  # 直近

    # 売買代金
    turnover = (latest.get("close_price", 0) or 0) * (latest.get("volume", 0) or 0)
    if turnover < 5_000_000:
        return False

    if side == "BUY":
        # 陽線
        if latest["close_price"] <= latest["open_price"]:
            return False
        # 高値ブレイク
        if latest["high_price"] <= prev1["high_price"]:
            return False
        return True

    elif side == "SELL":
        # 陰線
        if latest["close_price"] >= latest["open_price"]:
            return False
        # 安値ブレイク
        if latest["low_price"] >= prev1["low_price"]:
            return False
        return True

    return False
