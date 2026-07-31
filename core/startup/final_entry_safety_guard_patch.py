# ============================================================
# File   : core/startup/final_entry_safety_guard_patch.py
# Version: V13-FULLY-INLINED
# ------------------------------------------------------------
# V13:
#   - 発注直前の安全ガード (時間帯/流動性/直近反転/板) は
#     trading/handlers/entry_controller.py の _final_guard_* 群 (Ver2.9) へ
#     インライン化済みのため撤去した。
#
#   - 発見した不具合: このパッチの _unwrap_true_original は、汎用の
#     "_original" 属性を持つ関数なら何であれ無条件に辿ってしまっていた。
#     entry_daily_risk_runtime_patch / entry_liquidity_runtime_patch /
#     entry_summary_retry_rotation_runtime_patch も同じ汎用属性名を
#     自分の再ラップ判定に使っていたため、このパッチが後から install
#     されると、それらのガード/記憶ロジックが呼び出し連鎖から
#     silently 外れてしまっていた。本文化により、この不明瞭な
#     アンラップ処理自体を廃止した。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = True


def install() -> bool:
    return True


__all__ = ["install"]
