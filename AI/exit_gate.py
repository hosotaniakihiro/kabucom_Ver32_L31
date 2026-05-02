# ============================================================
# File   : AI/exit_gate.py
# Ver1.1-FINAL-AI-EXIT-GATE-WITH-SNAPSHOT
# ------------------------------------------------------------
# ✔ EXIT 最終ゲート（唯一の判断場所）
# ✔ ENTRY / EXIT 思想完全分離
# ✔ 即時利益 / ホールド時間 / 崩壊検知 の3層AI
# ✔ 副作用ゼロ（position / DB を直接触らない）
# ✔ 戻り値は ExitDecision or None
# ✔ ★ 未EXIT / EXIT 判定スナップショット保存対応
# ============================================================

import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from AI.exit_models.takeprofit import predict_exit_takeprofit
from AI.exit_models.holdtime import predict_exit_holdtime
from AI.exit_models.collapse import predict_exit_collapse

from trading.entry.exit_check_snapshotter import append_exit_snapshot

logger = logging.getLogger(__name__)


# ============================================================
# Exit Decision Schema（契約）
# ============================================================

@dataclass
class ExitDecision:
    reason: str           # AI_TAKE_PROFIT / AI_HOLDTIME / AI_COLLAPSE
    confidence: float     # 0.0 - 1.0
    detail: dict          # 補足情報（ログ・DB用）


# ============================================================
# AI EXIT GATE（唯一の公開API）
# ============================================================

def ai_exit_check(
    position,
    market_features: dict,
) -> Optional[ExitDecision]:
    """
    AI による EXIT 判断

    Args:
        position:
            - symbol
            - entry_price
            - entry_time
            - elapsed_seconds
            - entry_features（ENTRY時の特徴量）
        market_features:
            - EXIT時点の特徴量

    Returns:
        ExitDecision or None
    """

    # ------------------------------
    # Guard
    # ------------------------------
    if position is None or not market_features:
        return None

    elapsed = position.elapsed_seconds

    # ============================================================
    # Exit-C：崩壊検知AI（最優先）
    # ============================================================

    try:
        collapse_prob = predict_exit_collapse(market_features)
    except Exception:
        logger.exception("[EXIT AI] collapse prediction failed")
        collapse_prob = 0.0

    # ============================================================
    # Exit-A：即時利益AI（Take Profit）
    # ============================================================

    try:
        takeprofit_prob = predict_exit_takeprofit(market_features)
    except Exception:
        logger.exception("[EXIT AI] takeprofit prediction failed")
        takeprofit_prob = 0.0

    # ============================================================
    # Exit-B：ホールド時間AI（Time Exit）
    # ============================================================

    try:
        expected_hold = predict_exit_holdtime(position.entry_features)
    except Exception:
        logger.exception("[EXIT AI] holdtime prediction failed")
        expected_hold = None

    hold_ratio = None
    if expected_hold and expected_hold > 0:
        hold_ratio = elapsed / expected_hold

    # ============================================================
    # EXIT 判定（優先順位厳守）
    # ============================================================

    # ---- 崩壊 EXIT ----
    if collapse_prob >= 0.80:
        snapshot = {
            "ts": datetime.now().isoformat(),
            "decision": "AI_COLLAPSE",
            "collapse_prob": round(collapse_prob, 3),
            "takeprofit_prob": round(takeprofit_prob, 3),
            "expected_hold": expected_hold,
            "elapsed": elapsed,
            "hold_ratio": round(hold_ratio, 3) if hold_ratio else None,
        }
        append_exit_snapshot(position.symbol, snapshot)

        return ExitDecision(
            reason="AI_COLLAPSE",
            confidence=collapse_prob,
            detail={
                "elapsed": elapsed,
                "collapse_prob": collapse_prob,
            },
        )

    # ---- 利確 EXIT ----
    if takeprofit_prob >= 0.70:
        snapshot = {
            "ts": datetime.now().isoformat(),
            "decision": "AI_TAKE_PROFIT",
            "collapse_prob": round(collapse_prob, 3),
            "takeprofit_prob": round(takeprofit_prob, 3),
            "expected_hold": expected_hold,
            "elapsed": elapsed,
            "hold_ratio": round(hold_ratio, 3) if hold_ratio else None,
        }
        append_exit_snapshot(position.symbol, snapshot)

        return ExitDecision(
            reason="AI_TAKE_PROFIT",
            confidence=takeprofit_prob,
            detail={
                "elapsed": elapsed,
                "takeprofit_prob": takeprofit_prob,
            },
        )

    # ---- ホールド時間 EXIT ----
    if hold_ratio is not None and hold_ratio >= 1.15:
        snapshot = {
            "ts": datetime.now().isoformat(),
            "decision": "AI_HOLDTIME",
            "collapse_prob": round(collapse_prob, 3),
            "takeprofit_prob": round(takeprofit_prob, 3),
            "expected_hold": expected_hold,
            "elapsed": elapsed,
            "hold_ratio": round(hold_ratio, 3),
        }
        append_exit_snapshot(position.symbol, snapshot)

        return ExitDecision(
            reason="AI_HOLDTIME",
            confidence=min(1.0, hold_ratio / 2),
            detail={
                "elapsed": elapsed,
                "expected_hold": expected_hold,
                "ratio": hold_ratio,
            },
        )

    # ============================================================
    # NO EXIT（HOLD）
    # ============================================================

    snapshot = {
        "ts": datetime.now().isoformat(),
        "decision": "HOLD",
        "collapse_prob": round(collapse_prob, 3),
        "takeprofit_prob": round(takeprofit_prob, 3),
        "expected_hold": expected_hold,
        "elapsed": elapsed,
        "hold_ratio": round(hold_ratio, 3) if hold_ratio else None,
    }
    append_exit_snapshot(position.symbol, snapshot)

    return None
