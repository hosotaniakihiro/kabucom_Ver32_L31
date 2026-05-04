# scoring_rules.py
import pandas as pd

def score_ma_cross(prev_ma5, prev_ma25, ma5, ma25, scoring):
    """
    MA5とMA25のゴールデンクロス
    """
    if all(pd.notnull([prev_ma5, prev_ma25, ma5, ma25])):
        if prev_ma5 <= prev_ma25 and ma5 > ma25:
            return scoring.get("ma5_ma25_cross", 2), "MA5とMA25のゴールデンクロス"
    return 0, None


def score_macd_cross(prev_macd, prev_signal, macd, signal, scoring):
    """
    MACDゴールデンクロス
    """
    if pd.notnull(macd) and pd.notnull(signal) and pd.notnull(prev_macd) and pd.notnull(prev_signal):
        if prev_macd <= prev_signal and macd > signal:
            return scoring.get("macd_cross", 2), "MACDゴールデンクロス"
    return 0, None

# 👉 この他にも既存の条件（RSI反発、ストキャス反発、ボリンジャーバンド反発…）
# を順次関数化して追加していく形に整理できます。
