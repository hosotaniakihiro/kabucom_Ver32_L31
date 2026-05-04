# ============================================================
# score_reason_master.py
# BUY / SELL スコア理由 完全マスター
# ------------------------------------------------------------
# ・scoring_core / summary / stats / 表示 の共通基盤
# ・ここに存在しない reason は WARNING 対象
# ============================================================


# --- BUY / LONG ------------------------------------------------

BUY_SCORE_REASONS = [
    # --- direction / base trend ---
    "dir_up",                 # ★追加：上昇方向（ベーストレンド）

    # --- trend ---
    "ma5_ma25_cross",
    "ma_up",
    "perfect_order_event",
    "first_pullback",
    "breakout_high",

    # --- pullback / rebound ---
    "fib_rebound",
    "rebound_on_ma25",
    "bollinger_rebound",
    "bb_3sigma_rebound",

    # --- momentum ---
    "macd_cross",
    "rsi_rebound",
    "stoch_rebound",
    "rci_rising",
    "rci_trio_up",
    "rci9_uptrend",

    # --- volume / price ---
    "volume_spike",
    "volume_surge",
    "volume_price_breakout",
    "volume_zone_break",
    "vwap_break",
    "vwap_breakout",
    "tick_surge",
    "bull_candle_volume",

    # --- candle patterns ---
    "bullish_engulfing",
    "bullish_counterattack",
    "bullish_side_by_side",
    "bullish_mat_hold",
    "bullish_belt_hold",
    "bullish_harami",
    "bullish_breakaway",
    "bullish_kicker",
    "bullish_tweezer_bottom",
    "morning_star",
    "piercing_line",
    "hammer",
    "inverted_hammer",
    "dragonfly_doji",
    "rising_three_methods",
    "window_up",
    "gap_up_breakout",

    # --- strong combo ---
    "bull_big_combo",
    "lower_wick_low_zone",
    "lower_wick_rebound",

    # --- absolute ---
    "rsi_oversold_30",
    "bb_lower_touch",
]


# --- SELL / SHORT ---------------------------------------------

SELL_SCORE_REASONS = [
    # --- direction ---
    "dir_down",

    # --- trend / MA ---
    "ma_alignment_down",
    "ma5_downtrend",
    "ma5_below_ma25",
    "perfect_order_down",

    # --- momentum failure ---
    "macd_dc",
    "rsi_falling",

    # --- price / volume ---
    "below_ma75",
    "vwap_fail",
    "volume_drop",
    "volume_peak_out",
    "volume_price_breakdown",
    "volume_zone_breakdown",

    # --- reversal ---
    "reversal_penalty",
    "fib_reversal",
    "pullback_entry_down",
    "ma_reversal_after_touch_down",

    # --- breakdown ---
    "breakdown_3",
    "gap_down_breakdown",
    "bollinger_breakdown",
    "bb_3sigma_breakdown",

    # --- candle patterns ---
    "bearish_engulfing",
    "bearish_engulfing2",
    "dark_cloud_cover",
    "evening_star",
    "shooting_star",
    "three_black_crows",
    "hanging_man",
    "bearish_harami",
    "bearish_doji_star",
    "bearish_breakaway",
    "window_down",
    "gapdown_red",

    # --- absolute ---
    "rsi_overbought_70",
    "bb_upper_touch",
]


# --- ALL (表示・集計用) ---------------------------------------

ALL_SCORE_REASONS = BUY_SCORE_REASONS + SELL_SCORE_REASONS
