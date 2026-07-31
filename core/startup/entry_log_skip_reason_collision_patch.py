# ============================================================
# File   : core/startup/entry_log_skip_reason_collision_patch.py
# Version: V4-FULLY-INLINED
# ------------------------------------------------------------
# V4: 全機能を本文へインライン化済み、このファイルは撤去済み:
#   - _log_skip の reason衝突回避 + HARD_PRUNE_REASONS pending prune
#     -> trading/handlers/entry_controller.py の _log_skip (Ver2.7)
#   - range_5m_filter の RANKING min_pct 緩和
#     -> trading/filters/volatility_filter.py の
#        _range_5m_filter_from_entry_row (V6)
#   - entry_from_ranking/run_ranking_entry_pipeline 実行前の
#     stale RANKING pending 掃除
#     -> trading/ranking/entry_from_ranking.py の entry_from_ranking (V5.3)
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = True


def install() -> bool:
    return True


__all__ = ["install"]
