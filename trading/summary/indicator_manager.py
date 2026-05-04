# ============================================================
# indicator_manager.py（Ver F）
# ------------------------------------------------------------
#  - PUSH更新 → 軽量指標のみ更新
#  - Yahoo補完 → 全期間の重インジケータ再計算
#  - DB復元 → 全期間の重インジケータ再計算
#
# EX:
#   update_indicators(auto_mode="push", interval=1)
#   update_indicators(auto_mode="yahoo")
#   update_indicators(auto_mode="db")
# ============================================================

import logging
from trading.summary.summary_indicator_recalc import (
    recalc_light_indicators,
    recalc_all_indicators,
)

logger = logging.getLogger(__name__)


def update_indicators(auto_mode="push", interval=None):
    """
    auto_mode:
        "push"  → 軽量更新（interval 必須）
        "yahoo" → 全期間フル再計算
        "db"    → 全期間フル再計算（起動時）
    """

    if auto_mode == "push":
        if interval is None:
            logger.error("update_indicators(push) intervalが必要です")
            return
        logger.info(f"⚡ 軽量インジケータ更新 interval={interval}")
        recalc_light_indicators(interval)
        return

    if auto_mode in ("yahoo", "db"):
        logger.info("🔧 全インジケータ再計算（heavy）")
        recalc_all_indicators()
        return

    logger.error(f"無効な auto_mode: {auto_mode}")
