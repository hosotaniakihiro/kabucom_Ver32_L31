# ============================================================
# File   : core/startup/ranking_direction_entry_guard_patch.py
# Version: Ver02-FULLY-INLINED-AND-FIXED
# ------------------------------------------------------------
# Ver02:
#   - このパッチは _passes_side_filter / _passes_entry_side_filter /
#     _allow_candidate_side / _side_filter_ok への差し込みを想定していたが、
#     trading/handlers/entry_controller.py にそれらの関数は存在せず、
#     フォールバック先の run_entry_pipeline ラップも run_entry_pipeline に
#     存在しない "entries" 引数を探すだけで、実際には一度もガードが
#     適用されていなかった (デッドコード)。
#   - trading/handlers/entry_controller.py の _build_scored_candidates に
#     side確定後の正しい位置で _ranking_direction_guard_ok として
#     インライン化し、実際に機能するよう修正した (対象は RANKING由来候補のみ)。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = True


def install() -> bool:
    return True


__all__ = ["install"]
