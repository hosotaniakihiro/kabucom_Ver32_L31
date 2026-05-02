# ============================================================
# File   : trading/entry/summary_ai_entry_runner.py
# Version: PRODUCTION-STABLE-REV1.0-COMPAT-SHIM
# ------------------------------------------------------------
# 【概要】
#   旧 import パス互換用 shim。
#
# 【旧】
#   from trading.entry.summary_ai_entry_runner import run_summary_ai_entry_from_df
#
# 【新】
#   from trading.entry.summary_ai.runner import run_summary_ai_entry_from_df
#
# 【重要】
#   - 実処理は trading.entry.summary_ai パッケージへ分離
#   - 本ファイルは薄い re-export のみ
# ============================================================

from __future__ import annotations

from trading.entry.summary_ai.runner import (
    run_summary_ai_entry_from_df,
    run_summary_ai_entry,
    run_push_summary_ai_entry,
    run_ranking_summary_ai_entry,
)

__all__ = [
    "run_summary_ai_entry_from_df",
    "run_summary_ai_entry",
    "run_push_summary_ai_entry",
    "run_ranking_summary_ai_entry",
]