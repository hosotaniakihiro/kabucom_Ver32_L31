# ============================================================
# File   : core/startup/final_entry_duplicate_cooldown_patch.py
# Version: V2-FULLY-INLINED
# ------------------------------------------------------------
# V2: このファイルはどこからも import されておらず、完全なデッドコード
#     だった (import されない限り、末尾の自動 install() も一度も走らない)。
#     同一銘柄・同一方向への重複発注/inflight抑止ロジックは、本来意図
#     されていた安全機能として trading/handlers/entry_controller.py の
#     _dup_cooldown_* 群 (Ver2.9) へインライン化し、実際に有効化した。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = True


def install() -> bool:
    return True


__all__ = ["install"]
