# ============================================================
# final_decision.py
# ENTRY × HOLDTIME × EXIT 統合判断（FINAL）
# ============================================================

import logging

from AI.train.holdtime.holdtime_lgbm import predict_hold_seconds
from AI.train.horizon.horizon_lgbm import predict_best_horizon

logger = logging.getLogger(__name__)


def decide_exit_action(features: dict) -> dict:
    """
    ENTRY 後の最終判断（HOLDTIME × EXIT）

    Returns:
        {
            "ok": bool,              # この判断を採用するか
            "hold_seconds": int,     # 推奨保有秒数
            "horizon": int,          # 利確目標ホライズン
            "reason": str,           # 判断理由（ログ / 学習用）
        }
    """

    # ----------------------------------------
    # AI 推論（安全実行）
    # ----------------------------------------
    try:
        hold_sec = int(predict_hold_seconds(features))
    except Exception:
        logger.exception("[FINAL_DECISION] holdtime prediction failed")
        return {
            "ok": False,
            "hold_seconds": 0,
            "horizon": 0,
            "reason": "holdtime_predict_error",
        }

    try:
        horizon = int(predict_best_horizon(features))
    except Exception:
        logger.exception("[FINAL_DECISION] horizon prediction failed")
        return {
            "ok": False,
            "hold_seconds": hold_sec,
            "horizon": 0,
            "reason": "horizon_predict_error",
        }

    # ----------------------------------------
    # 安全ガード（最重要）
    # ----------------------------------------
    # 極短期 → 即スキャ or 即EXIT
    if hold_sec <= 10:
        return {
            "ok": True,
            "hold_seconds": 0,
            "horizon": 0,
            "reason": "holdtime_too_short",
        }

    # horizon が意味を持たない
    if horizon <= 0:
        return {
            "ok": False,
            "hold_seconds": hold_sec,
            "horizon": 0,
            "reason": "invalid_horizon",
        }

    # ----------------------------------------
    # 正常系
    # ----------------------------------------
    return {
        "ok": True,
        "hold_seconds": hold_sec,
        "horizon": horizon,
        "reason": "ai_accept",
    }
