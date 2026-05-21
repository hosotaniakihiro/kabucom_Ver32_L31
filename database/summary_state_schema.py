# ============================================================
# File   : database/summary_state_schema.py
# Version: Ver02-SUMMARY-WIDE-PERSISTENCE-COLUMNS
# ------------------------------------------------------------
# stock_summary_1min / 3min / 5min の起動時不足列補完用。
# database/__init__.py がこの定義を database.session.SUMMARY_BOOTSTRAP_COLUMNS
# へ登録し、database/session.py が毎回 PRAGMA table_info で不足列だけ追加する。
# ============================================================

from __future__ import annotations


SUMMARY_STATE_BOOTSTRAP_COLUMNS: list[tuple[str, str]] = [
    # tick / raw metadata
    ("tick_count", "REAL DEFAULT 0"),
    ("first_tick_at", "TEXT"),
    ("last_tick_at", "TEXT"),
    ("price", "REAL"),
    ("current_price", "REAL"),
    ("date", "TEXT"),
    ("time", "TEXT"),
    ("start_time", "TEXT"),
    ("end_time", "TEXT"),
    ("time_range", "TEXT"),

    # reason / display diagnostics
    ("buy_reason_ja", "TEXT"),
    ("sell_reason_ja", "TEXT"),
    ("exit_reason_ja", "TEXT"),
    ("usable_technical_ready", "INTEGER DEFAULT 0"),

    # AI gate persistence
    ("ai_passed", "INTEGER DEFAULT 0"),
    ("ai_buy_passed", "INTEGER DEFAULT 0"),
    ("ai_sell_passed", "INTEGER DEFAULT 0"),
    ("ai_exit_passed", "INTEGER DEFAULT 0"),
    ("ai_reason", "TEXT"),
    ("ai_exit_reason", "TEXT"),
    ("ai_decision", "TEXT"),
    ("ai_exit_decision", "TEXT"),
    ("ai_side", "TEXT"),
    ("ai_confidence", "REAL"),
    ("ai_exit_confidence", "REAL"),

    # score breakdown / aliases
    ("score_penalty", "REAL DEFAULT 0"),
    ("breakdown_base", "REAL DEFAULT 0"),
    ("breakdown_trend", "REAL DEFAULT 0"),
    ("breakdown_mom", "REAL DEFAULT 0"),
    ("breakdown_vel", "REAL DEFAULT 0"),
    ("breakdown_pen", "REAL DEFAULT 0"),

    # ranking join persistence columns
    ("ranking", "TEXT"),
    ("ranking_type", "TEXT"),
    ("rank", "REAL"),
    ("rank_no", "REAL"),
    ("change_rate", "REAL"),
    ("chg", "REAL"),
    ("turnover", "REAL"),
    ("turn", "REAL"),
    ("trading_volume", "REAL"),
    ("ranking_score", "REAL DEFAULT 0"),
    ("ranking_score_total", "REAL DEFAULT 0"),
    ("market", "TEXT"),
    ("exchange_name", "TEXT"),

    # MA cross continuation state
    ("ma_cross_state", "TEXT"),
    ("ma_cross_score_delta", "REAL DEFAULT 0"),
    ("ma_cross_reasons", "TEXT"),
    ("ma5_above_ma25", "INTEGER DEFAULT 0"),
    ("ma5_below_ma25", "INTEGER DEFAULT 0"),
    ("ma5_above_ma25_bars", "INTEGER DEFAULT 0"),
    ("ma5_below_ma25_bars", "INTEGER DEFAULT 0"),
    ("ma5_ma25_gap_pct", "REAL DEFAULT 0"),
    ("ma5_ma25_gap_pct_prev", "REAL DEFAULT 0"),
    ("ma5_ma25_gap_widening", "INTEGER DEFAULT 0"),
    ("ma5_ma25_gap_shrinking", "INTEGER DEFAULT 0"),
    ("ma25_ma75_gap_pct", "REAL DEFAULT 0"),
    ("ma25_ma75_gap_pct_prev", "REAL DEFAULT 0"),
    ("ma25_ma75_gap_widening", "INTEGER DEFAULT 0"),
    ("ma25_ma75_gap_shrinking", "INTEGER DEFAULT 0"),
    ("ma_stack_bullish", "INTEGER DEFAULT 0"),
    ("ma_stack_bearish", "INTEGER DEFAULT 0"),
    ("golden_cross_recent", "INTEGER DEFAULT 0"),
    ("golden_cross_continuation", "INTEGER DEFAULT 0"),
    ("golden_cross_mature", "INTEGER DEFAULT 0"),
    ("golden_cross_exhaustion", "INTEGER DEFAULT 0"),
    ("dead_cross_recent", "INTEGER DEFAULT 0"),
    ("dead_cross_continuation", "INTEGER DEFAULT 0"),
    ("dead_cross_mature", "INTEGER DEFAULT 0"),
    ("dead_cross_exhaustion", "INTEGER DEFAULT 0"),

    # VWAP continuation state
    ("vwap_state", "TEXT"),
    ("vwap_score_delta", "REAL DEFAULT 0"),
    ("vwap_reasons", "TEXT"),
    ("price_above_vwap", "INTEGER DEFAULT 0"),
    ("price_below_vwap", "INTEGER DEFAULT 0"),
    ("price_above_vwap_bars", "INTEGER DEFAULT 0"),
    ("price_below_vwap_bars", "INTEGER DEFAULT 0"),
    ("vwap_gap_pct", "REAL DEFAULT 0"),
    ("vwap_gap_pct_prev", "REAL DEFAULT 0"),
    ("vwap_gap_widening", "INTEGER DEFAULT 0"),
    ("vwap_gap_shrinking", "INTEGER DEFAULT 0"),
    ("above_vwap_recent", "INTEGER DEFAULT 0"),
    ("above_vwap_continuation", "INTEGER DEFAULT 0"),
    ("above_vwap_mature", "INTEGER DEFAULT 0"),
    ("above_vwap_exhaustion", "INTEGER DEFAULT 0"),
    ("below_vwap_recent", "INTEGER DEFAULT 0"),
    ("below_vwap_continuation", "INTEGER DEFAULT 0"),
    ("below_vwap_mature", "INTEGER DEFAULT 0"),
    ("below_vwap_exhaustion", "INTEGER DEFAULT 0"),
    ("vwap_neutral_zone", "INTEGER DEFAULT 0"),
    ("vwap_cross_count_10", "INTEGER DEFAULT 0"),
    ("vwap_choppy", "INTEGER DEFAULT 0"),
    ("vwap_stable_above", "INTEGER DEFAULT 0"),
    ("vwap_stable_below", "INTEGER DEFAULT 0"),
    ("vwap_entry_block", "INTEGER DEFAULT 0"),
]


__all__ = ["SUMMARY_STATE_BOOTSTRAP_COLUMNS"]
