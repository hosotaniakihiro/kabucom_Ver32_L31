# trading/summary/pattern_mapping.py
"""
日本語パターン名 → score_config.ini の英語キー 変換辞書
"""

BULLISH_MAPPING = {
    "赤三兵": "bull_big_combo",
    "明けの明星": "morning_star",
    "抱き陽線": "bullish_engulfing",
    "切り込み線": "piercing_line",
    "たくり線": "hammer",
    "上窓": "window_up",
    "捨て子底": "bullish_belt_hold",
    "勢力線": "inverted_hammer",
    "やぐら底": "dragonfly_doji",
    "陽のはらみ": "bullish_harami",
    "上げ三法": "rising_three_methods",
    "逆襲線": "bullish_kicker",
    "上げタスキ": "bullish_tasuki",
    "下げの下ひげ": "lower_wick_low_zone",
    "毛抜き底": "bullish_tweezer_bottom",
    "窓開け後の陽線継続": "bullish_breakaway",
    "押え込み線": "bullish_counterattack",
    "並び赤": "bullish_side_by_side",
    "上伸途上の連続タスキ": "bullish_mat_hold",
    "大陽線": "bull_candle_volume",
}

BEARISH_MAPPING = {
    "三羽烏": "bear_big_combo",
    "宵の明星": "evening_star",
    "抱き陰線": "bearish_engulfing",
    "かぶせ線": "dark_cloud_cover",
    "首吊り線": "hanging_man",
    "下窓": "window_down",
    "化け線": "bearish_belt_hold",
    "上ヒゲ陰線": "shooting_star",
    "毛抜き天井": "bearish_tweezer_top",
    "二羽ガラス": "upside_gap_two_crows",
    "弱気キッカー": "bearish_kicker",
    "行き詰まり線": "bearish_counterattack",
    "陰線並び": "bearish_side_by_side",
    "下げ三法": "falling_three_methods",
    "下落途上の連続タスキ": "bearish_mat_hold",
    "陰のはらみ": "bearish_harami",
    "陰のコマ天井": "bearish_doji_star",
    "下げタスキ": "bearish_tasuki",
    "下放れ三手": "bearish_breakaway",
    "大陰線": "upper_wick_bear",
}
