# ============================================================
# trading/entry/ai_score.py
# ------------------------------------------------------------
# ✔ AI予測 → スコア変換
# ✔ ENTRY決定はしない
# ✔ 重みは設定で変更可能
# ============================================================

from typing import Dict, Optional

# 固定重み（まずは手動）
AI_WEIGHTS = {
    "1M":  1.5,
    "2M":  1.0,
    "10S": 0.5,
}

def apply_ai_score(
    base_score: float,
    ai_preds: Dict[str, Optional[float]],
) -> float:
    score = base_score

    for tf, weight in AI_WEIGHTS.items():
        p = ai_preds.get(tf)
        if p is None:
            continue
        score += weight if p > 0 else -weight

    return score


def is_blocked_by_1s(ai_1s: Optional[float]) -> bool:
    """
    1秒足は暴発防止ブレーキ専用
    """
    return ai_1s is not None and ai_1s < -0.5
