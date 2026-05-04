# ============================================================
# label_translations.py（Ver24-FINAL – SINGLE LABEL MAP）
# ------------------------------------------------------------
# ・BUY / SELL / Pattern / Ranking を完全統合
# ・英語 label → 日本語表示 のみを担当
# ・score / decision / AI 学習すべて共通
# ============================================================


# ============================================================
# 🔤 英語ラベル → 日本語表示
# ============================================================
LABEL_JA = {

    # ========================================================
    # 🧭 方向性（direction）
    # ========================================================
    "dir_up": "上昇方向",
    "dir_down": "下降方向",

    # ========================================================
    # 📈 BUY / 上昇系 technical
    # ========================================================
    "macd_gc": "MACDゴールデンクロス",
    "macd_cross": "MACDゴールデンクロス",
    "rsi_ok": "RSI良好",
    "rsi_rebound": "RSI反発",
    "rci_rising": "RCI上昇",
    "rci9_uptrend": "RCI9上昇トレンド",

    "ma_alignment": "MA順列（上昇）",
    "ma_uptrend": "上昇トレンド",
    "ma5_ma25_cross": "MA5・MA25ゴールデンクロス",
    "perfect_order": "パーフェクトオーダー上昇",

    "bb_expand": "ボリンジャーバンド拡大",
    "bollinger_rebound": "ボリンジャーバンド反発",
    "bollinger_breakout": "ボリンジャーバンド上抜け",
    "bb_3sigma_rebound": "BB -3σ反発",

    "atr_spike": "ATR急拡大",
    "vwap_break": "VWAP上抜け",
    "vwap_breakout": "VWAP上抜け",
    "volume_spike": "出来高急増",
    "volume_surge": "出来高急増",
    "volume_price_breakout": "出来高伴う上昇",
    "volume_zone_break": "出来高ゾーン上抜け",
    "gc_volume_boost": "出来高を伴うGC",

    "green_series": "連続陽線",
    "bullish_streak": "連続陽線",
    "tick_surge": "ティック急増",

    "pullback_entry": "押し目買い",
    "rebound_on_ma25": "MA25反発",

    # ========================================================
    # 🕯 BUY candlestick / pattern
    # ========================================================
    "engulf_bull": "陽線包み足",
    "bullish_engulfing": "陽線包み足",
    "engulfing_reversal": "包み足反転",
    "small_pullback": "小押し目",
    "breakout_3": "3本高値ブレイク",

    "morning_star": "明けの明星",
    "piercing_line": "差し込み線",
    "hammer": "ハンマー",
    "inverted_hammer": "逆カラカサ",
    "dragonfly_doji": "トンボ足",
    "bullish_harami": "陽の孕み足",
    "rising_three_methods": "上昇三法",
    "bullish_kicker": "ブルキッカー",
    "bullish_tasuki": "たすき上げ",
    "bullish_tweezer_bottom": "毛抜き底",
    "bullish_breakaway": "上昇ブレイクアウェイ",
    "bullish_counterattack": "ブルカウンター",
    "bullish_side_by_side": "並び赤",
    "bullish_mat_hold": "マットホールド陽線",

    "lower_wick_low_zone": "下ヒゲ安値圏",
    "bull_big_combo": "強気コンボ",
    "window_up": "窓開け上昇",
    "fib_rebound": "フィボナッチ反発",

    # ========================================================
    # ⚠️ 注意・警戒（BUY 側で減点・状態把握用）
    # ========================================================
    "ma5_downtrend": "MA5下降（警戒）",
    "ma5_below_ma25": "MA5がMA25下",
    "ma_reversal_after_touch": "MAタッチ後反発",

    # ========================================================
    # 📉 SELL / 下落系 technical
    # ========================================================
    "macd_dc": "MACDデッドクロス",
    "macd_dead_cross": "MACDデッドクロス",
    "rsi_falling": "RSI低下",
    "rsi_down": "RSI下落",
    "rci_falling": "RCI下落",

    "ma_alignment_down": "MA順列（下降）",
    "ma_downtrend": "下降トレンド",
    "perfect_order_down": "パーフェクトオーダー下降",
    "below_ma75": "MA75下割れ",

    "bb_expand_down": "ボリンジャーバンド拡大（下降）",
    "bollinger_reversal": "ボリンジャーバンド反落",
    "bollinger_breakdown": "ボリンジャーバンド下抜け",
    "bb_3sigma_breakdown": "BB -3σ下抜け",

    "vwap_fail": "VWAP割れ",
    "vwap_breakdown": "VWAP下抜け",

    "volume_drop": "出来高減少",
    "volume_peak_out": "出来高ピークアウト",
    "volume_price_breakdown": "出来高伴う下落",
    "volume_zone_breakdown": "出来高ゾーン下抜け",
    "dc_volume_boost": "出来高を伴うDC",

    "red_series": "連続陰線",
    "bearish_streak": "連続陰線",
    "pullback_entry_down": "戻り売り",
    "rebound_from_ma75": "MA75反落",

    # ========================================================
    # 🕯 SELL candlestick / pattern
    # ========================================================
    "engulf_bear": "陰線包み足",
    "bearish_engulfing": "陰線包み足",
    "bearish_engulfing2": "強陰線包み足",
    "dark_cloud_cover": "黒雲",
    "evening_star": "宵の明星",
    "shooting_star": "流れ星",
    "upper_wick_bear": "上ヒゲ陰線",
    "upper_wick_high_zone": "上ヒゲ高値圏",

    "three_black_crows": "三羽ガラス",
    "hanging_man": "首吊り線",
    "window_down": "窓開け下落",
    "bearish_belt_hold": "ベルトホールド陰線",
    "bearish_tweezer_top": "毛抜き天井",
    "upside_gap_two_crows": "二羽ガラス",
    "bearish_kicker": "ベアキッカー",
    "bearish_counterattack": "カウンターアタック陰線",
    "bearish_side_by_side": "並び黒",
    "falling_three_methods": "下降三法",
    "bearish_mat_hold": "マットホールド陰線",
    "bearish_harami": "陰の孕み足",
    "bearish_doji_star": "陰のコマ星",
    "bearish_tasuki": "たすき下げ",
    "bearish_breakaway": "下落ブレイクアウェイ",

    "bear_big_combo": "弱気コンボ",
    "upper_wick_long": "長い上ヒゲ",
    "upper_wick_series": "上ヒゲ連発",
    "red_series_3": "3連続陰線",
    "big_red": "大陰線",
    "gapdown_red": "ギャップダウン陰線",

    "fib_reversal": "フィボナッチ反落",

    # ========================================================
    # 🏆 ランキング由来（ranking_score）
    # ========================================================
    "rank_volume_top10": "出来高ランキング上位",
    "rank_price_up_top10": "値上がり率ランキング上位",
    "rank_price_down_top10": "値下がり率ランキング上位",
}


# ============================================================
# 🕯 patterns.py 用（完全互換）
# ============================================================
CANDLE_PATTERN_JA = LABEL_JA.copy()


# ============================================================
# 🔄 legacy / decision.py 互換
# ============================================================
LABEL_JA_SHORT = LABEL_JA
