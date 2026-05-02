# ============================================================
# File   : trading/exit/final_exit_judge.py
# Version: Ver1.0-PRODUCTION-FINAL-EXIT-JUDGE-UNIFIED
# ------------------------------------------------------------
# ✔ 全Exit統合
# ✔ 優先順位制御
# ✔ RL / AI / rule融合
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def judge_final_exit(
    position: Dict[str, Any],
    context: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    try:

        # ====================================================
        # ① 強制Exit（最優先）
        # ====================================================

        # crash
        if context.get("crash_signal"):
            return {"action": "CRASH_EXIT"}

        # loss guard
        if context.get("loss_guard"):
            return {"action": "LOSS_GUARD_EXIT"}

        # ====================================================
        # ② AI Exit
        # ====================================================

        ai_exit_prob = context.get("ai_exit_prob", 0.0)

        if ai_exit_prob > 0.8:
            return {"action": "AI_EXIT"}

        # ====================================================
        # ③ RL Exit
        # ====================================================

        rl_action = context.get("rl_action")

        if rl_action == "EXIT":
            return {"action": "RL_EXIT"}

        # ====================================================
        # ④ collapse（急落検知）
        # ====================================================

        if context.get("collapse_signal"):
            return {"action": "COLLAPSE_EXIT"}

        # ====================================================
        # ⑤ トレーリング
        # ====================================================

        if context.get("trailing_exit"):
            return {"action": "TRAILING_EXIT"}

        # ====================================================
        # ⑥ 通常ルール
        # ====================================================

        pnl = context.get("pnl", 0.0)

        if pnl >= 0.03:
            return {"action": "TAKE_PROFIT"}

        if pnl <= -0.015:
            return {"action": "STOP_LOSS"}

        # ====================================================
        # ⑦ トレンド崩壊
        # ====================================================

        trend = context.get("_score_trend", 0)
        momentum = context.get("_score_momentum", 0)

        if trend < -0.3 and momentum < 0:
            return {"action": "TREND_BREAK"}

        return None

    except Exception:
        logger.exception("[final_exit_judge] failed")
        return None