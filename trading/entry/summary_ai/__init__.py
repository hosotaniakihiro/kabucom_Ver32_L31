# ============================================================
# File   : trading/entry/summary_ai/__init__.py
# Version: PRODUCTION-STABLE-REV1.2-SAFE-PACKAGE-INIT-SIGNAL-FILTER
# ============================================================

from __future__ import annotations

__version__ = "PRODUCTION-STABLE-REV1.2-SAFE-PACKAGE-INIT-SIGNAL-FILTER"

try:
    from .runner import (
        run_summary_ai_entry_from_df,
        run_summary_ai_entry,
        run_push_summary_ai_entry,
        run_ranking_summary_ai_entry,
    )
    # weak_signal_filter_patch は runner.run_ai_gate_for_candidates 本体へ
    # 本文化した (_wsf_*)。import 時点で効くので install は不要。
except Exception:
    run_summary_ai_entry_from_df = None
    run_summary_ai_entry = None
    run_push_summary_ai_entry = None
    run_ranking_summary_ai_entry = None


__all__ = [
    "__version__",
    "run_summary_ai_entry_from_df",
    "run_summary_ai_entry",
    "run_push_summary_ai_entry",
    "run_ranking_summary_ai_entry",
]
