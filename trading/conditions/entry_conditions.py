# trading/summary/entry_conditions.py
import pandas as pd
import logging
# ... 既存の import 群

logger = logging.getLogger(__name__)
def check_buy_entry_condition(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 3:
        print("❌ BUY条件: データ不足")
        return False

    last3 = df.tail(3).reset_index(drop=True)
    c1, c2, c3 = last3.iloc[0], last3.iloc[1], last3.iloc[2]

    turnover_prev = (c2["close_price"] or 0) * (c2["volume"] or 0)

    cond1 = c3["high_price"] > c1["high_price"]
    cond2 = c2["close_price"] > c2["open_price"]
    cond3 = turnover_prev >= 5_000_000

    print(f"[DEBUG BUY] {c1['symbol'] if 'symbol' in c1 else ''} "
          f"cond1(高値超え)={cond1}, cond2(陽線)={cond2}, cond3(売買代金)={cond3}, "
          f"turnover_prev={turnover_prev:,.0f}")

    return cond1 and cond2 and cond3


def check_sell_entry_condition(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 3:
        print("❌ SELL条件: データ不足")
        return False

    last3 = df.tail(3).reset_index(drop=True)
    c1, c2, c3 = last3.iloc[0], last3.iloc[1], last3.iloc[2]

    turnover_prev = (c2["close_price"] or 0) * (c2["volume"] or 0)

    cond1 = c3["low_price"] < c1["low_price"]
    cond2 = c2["close_price"] < c2["open_price"]
    cond3 = turnover_prev >= 5_000_000

    print(f"[DEBUG SELL] {c1['symbol'] if 'symbol' in c1 else ''} "
          f"cond1(安値割れ)={cond1}, cond2(陰線)={cond2}, cond3(売買代金)={cond3}, "
          f"turnover_prev={turnover_prev:,.0f}")

    return cond1 and cond2 and cond3
