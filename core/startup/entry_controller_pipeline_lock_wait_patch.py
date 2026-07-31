# ============================================================
# File   : core/startup/entry_controller_pipeline_lock_wait_patch.py
# Version: V7-FULLY-INLINED
# ------------------------------------------------------------
# V7: 全機能を trading/handlers/entry_controller.py の run_entry_pipeline
#     本体 (Ver2.8) へインライン化済み、このファイルは撤去済み:
#   - RANKING/TONOSAMA/SUMMARY 向け lock-wait
#   - lock timeout 時の古い pending prune + stale lock reset
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = True


def install() -> bool:
    return True


__all__ = ["install"]
