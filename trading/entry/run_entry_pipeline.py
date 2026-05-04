# ============================================================
# File   : trading/entry/run_entry_pipeline.py
# Function:
#   - entry pipeline の後方互換 facade
#   - 旧 import パスを維持する
#   - 実装本体は trading.entry.pipeline.* に分割
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-FACADE
# ------------------------------------------------------------
# ✔ 旧 import 互換維持
# ✔ run_entry_pipeline を再公開
# ✔ run_summary_entry を再公開
# ✔ run_ai_entry を再公開
# ✔ run_summary_ai_entry を再公開
# ✔ run_combined_ai_entry を再公開
# ✔ 本体は trading.entry.pipeline.router / legacy_entry へ移動
# ============================================================

from __future__ import annotations

from trading.entry.pipeline.router import (
    run_entry_pipeline,
    run_summary_ai_entry,
    run_combined_ai_entry,
)

from trading.entry.pipeline.legacy_entry import (
    run_summary_entry,
    run_ai_entry,
)

__all__ = [
    "run_entry_pipeline",
    "run_summary_entry",
    "run_ai_entry",
    "run_summary_ai_entry",
    "run_combined_ai_entry",
]