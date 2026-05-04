# candle_patterns_eval.py
from candlestick_patterns import detect_bullish_patterns

def score_candlestick_patterns(df_symbol):
    """
    ローソク足パターン検出（買い用）
    """
    reasons = []
    score = 0
    try:
        patterns = detect_bullish_patterns(df_symbol)
        if isinstance(patterns, str):
            patterns = [patterns]
        elif not isinstance(patterns, list):
            patterns = []

        for p in patterns:
            points = 2
            score += points
            reasons.append(f"🟢 {p} (+{points})")

    except Exception as e:
        reasons.append(f"⚠️ ローソク足パターン判定エラー: {e}")

    return score, reasons
def score_candlestick_patterns_short(df_symbol):
    """
    ローソク足パターン検出（売り用）
    """
    reasons = []
    score = 0
    try:
        patterns = detect_bearish_patterns(df_symbol)
        if isinstance(patterns, str):
            patterns = [patterns]
        elif not isinstance(patterns, list):
            patterns = []

        for p in patterns:
            points = -2
            score += points
            reasons.append(f"🔴 {p} ({points})")

    except Exception as e:
        reasons.append(f"⚠️ ローソク足パターン判定エラー: {e}")

    return score, reasons
