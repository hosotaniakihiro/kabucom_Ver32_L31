# ============================================================
# File   : core/startup/final_entry_liquidity_movement_hard_guard_patch.py
# Version: V6-FULLY-INLINED
# ------------------------------------------------------------
# V6: 出来高/値動きハードガード (SUMMARY_AI高スコア救済つき) は
#     trading/handlers/entry_controller.py の _hard_guard_* 群 (Ver2.9) へ
#     インライン化済みのため撤去した。
# ============================================================
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
VERSION = "V6-FULLY-INLINED"
_INSTALLED = True


def install() -> bool:
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY HARD GUARD] auto install failed")

__all__ = ["install", "VERSION"]
