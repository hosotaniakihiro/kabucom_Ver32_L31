# trading/utils_summary_format.py

# 条件キー → 日本語ラベル変換
REASON_LABELS = {
    # --- BUY ---
    "pullback_entry": "押し目買い",
    "bollinger_rebound": "ボリンジャーバンド反発",
    "bollinger_breakout": "ボリンジャーバンド上抜け",
    "bb_3sigma_rebound": "BB -3σ反発",
    "gap_up_breakout": "ギャップアップブレイク",
    "ma5_ma25_cross": "MA5とMA25ゴールデンクロス",
    "macd_cross": "MACDゴールデンクロス",
    "rsi_rebound": "RSI反発",
    "stoch_rebound": "ストキャス反発",
    "ma_uptrend": "MA上昇トレンド",
    "perfect_order": "パーフェクトオーダー",
    "gc_volume_boost": "ゴールデンクロス後の出来高増",
    "volume_surge": "出来高急増",
    "volume_zone_break": "出来高ゾーン上抜け",
    "vwap_breakout": "VWAP上抜け",
    "rci_trio_up": "RCI上昇3連続",
    "bullish_engulfing": "強気包み足",
    "engulfing_reversal": "包み足反転",
    "bull_candle_volume": "大陽線＋出来高増",
    "fib_rebound": "フィボナッチ反発",
    "tick_surge": "ティック急増",
    "lower_wick_low_zone": "安値圏で下ヒゲ陽線",

    # --- SELL ---
    "vwap_breakdown": "VWAP割れ",
    "volume_peak_out": "出来高ピークアウト",
    "volume_price_breakdown": "出来高伴う下落",
    "gap_down_breakdown": "ギャップダウン下落",
    "ma_dead_cross": "MAデッドクロス",
    "ma_downtrend": "MA下降トレンド",
    "perfect_order_down": "下降パーフェクトオーダー",
    "ma5_below_ma25": "MA5がMA25下回り",
    "ma_reversal_after_touch_down": "MA接触後の反落",
    "macd_dead_cross": "MACDデッドクロス",
    "rsi_down": "RSI下落",
    "rci_trio_down": "RCI下落3連続",
    "stoch_dc": "ストキャスデッドクロス",
    "bollinger_reversal": "ボリンジャーバンド反落",
    "bollinger_breakdown": "ボリンジャーバンド下抜け",
    "bb_3sigma_breakdown": "BB -3σ割れ",
    "dc_volume_boost": "デッドクロス後の出来高増",
    "volume_zone_breakdown": "出来高ゾーン下抜け",
    "bearish_streak": "陰線3連続",
    "bearish_engulfing": "弱気包み足",
    "dark_cloud_cover": "黒雲型（Dark Cloud Cover）",
    "evening_star": "宵の明星",
    "shooting_star": "射撃星",
    "upper_wick_bear": "上ヒゲ陰線",
    "upper_wick_high_zone": "高値圏での上ヒゲ",
    "fib_reversal": "フィボナッチ反落",
}


def format_reasons(reasons: list[str], side: str = "BUY") -> str:
    """
    英語キー＋スコアを日本語ラベルに変換
    - BUY条件成立: bollinger_rebound (+1.0)
      → ボリンジャーバンド反発 (+1)
    """
    if not reasons:
        return "なし"

    formatted = []
    for r in reasons:
        key, score_val = None, None

        if "(" in r:
            # "BUY条件成立: bollinger_rebound (+1.0)"
            try:
                left, score = r.rsplit("(", 1)
                key = left.split(":")[-1].strip()
                score_val = score.rstrip(")")
            except Exception:
                key = r
        else:
            key = r

        jp = REASON_LABELS.get(key, key)

        if score_val:
            try:
                val = int(float(score_val.replace("+", "").replace("−", "-")))
                formatted.append(f"{jp} ({val:+d})")
            except Exception:
                formatted.append(f"{jp} ({score_val})")
        else:
            formatted.append(jp)

    return " / ".join(formatted)
