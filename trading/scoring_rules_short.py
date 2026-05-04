# scoring_rules_short.py
import pandas as pd

def score_ma_cross_short(prev_ma5, prev_ma25, ma5, ma25, scoring):
    """MA5とMA25のデッドクロス"""
    if all(pd.notnull([prev_ma5, prev_ma25, ma5, ma25])):
        if prev_ma5 >= prev_ma25 and ma5 < ma25:
            return scoring.get("ma5_ma25_cross", -2), "MA5とMA25のデッドクロス"
    return 0, None


def score_macd_cross_short(prev_macd, prev_signal, macd, signal, scoring):
    """MACDデッドクロス"""
    if pd.notnull(macd) and pd.notnull(signal) and pd.notnull(prev_macd) and pd.notnull(prev_signal):
        if prev_macd >= prev_signal and macd < signal:
            return scoring.get("macd_cross", -2), "MACDデッドクロス"
    return 0, None
