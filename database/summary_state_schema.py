# ============================================================
# File   : database/summary_state_schema.py
# Version: Ver01-SUMMARY-STATE-INDICATOR-COLUMNS
# ------------------------------------------------------------
# summary DB の stock_summary_1min / 3min / 5min に、
# 起動時 bootstrap で一括作成する状態指標カラム定義。
#
# database/session.py から import され、
# SUMMARY_BOOTSTRAP_COLUMNS に追加される。
#
# 目的:
#   - runtime patch の ALTER TABLE に頼らず、システム立ち上げ時に不足列を作成
#   - MAクロス状態 / VWAP状態 / VWAP行き来対策を DB schema として正式管理
# ============================================================

from __future__ import annotations


SUMMARY_STATE_BOOTSTRAP_COLUMNS: list[tuple[str, str]] = [
    # --------------------------------------------------------
    # MA cross continuation state
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # VWAP continuation state
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # VWAP choppy / neutral-zone guard columns
    # --------------------------------------------------------
    # VWAP付近を株価が行き来する場合のもみ合い判定用。
    # 計算処理は別途 vwap_state_summary_patch 側で拡張する。
    # DB列は先に起動時一括作成しておく。
    ("vwap_neutral_zone", "INTEGER DEFAULT 0"),
    ("vwap_cross_count_10", "INTEGER DEFAULT 0"),
    ("vwap_choppy", "INTEGER DEFAULT 0"),
    ("vwap_stable_above", "INTEGER DEFAULT 0"),
    ("vwap_stable_below", "INTEGER DEFAULT 0"),
    ("vwap_entry_block", "INTEGER DEFAULT 0"),
]


__all__ = ["SUMMARY_STATE_BOOTSTRAP_COLUMNS"]
