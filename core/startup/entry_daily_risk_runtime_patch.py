# ============================================================
# File   : core/startup/entry_daily_risk_runtime_patch.py
# Version: V2-FULLY-INLINED-AND-FIXED
# ------------------------------------------------------------
# V2:
#   - 日次リスクガード (銘柄別上限/日次損失上限/連敗停止/勝ち銘柄再エントリー許可) と
#     record_exit_event 側の実現損益記録は、trading/handlers/entry_controller.py の
#     _daily_risk_* 群 (Ver2.9) + trading/exit/symbol_trade_guard.py の
#     record_exit_event へインライン化済みのため撤去した。
#
#   - 発見した不具合: このガードは final_entry_safety_guard_patch.py の
#     _unwrap_true_original が汎用の "_original" 属性を無条件に辿ってしまうため、
#     main.py の起動順 (このパッチが先、final_entry_safety_guard_patch が後) では
#     毎回確実にこのガードだけが読み飛ばされ、一度も機能していなかった。
#     本文化にあたり修復し、実際に有効化した。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = True


def install() -> bool:
    return True


__all__ = ["install"]
