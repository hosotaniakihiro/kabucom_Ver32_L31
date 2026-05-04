# ============================================================
# AI/entry_probability.py
# ------------------------------------------------------------
# ✔ スコア・理由・市場状態から ENTRY 勝率を推定
# ✔ 既存ルール（score_config.ini）を一切破壊しない
# ✔ entry_gate 用「参考確率」だけを返す（可否判断しない）
# ✔ 将来 ML / NN（model_*.pkl）に差し替え可能
# ============================================================

import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# ============================================================
# 🔧 設定
# ============================================================

# ML を使うか（False = 完全ヒューリスティック）
USE_ML_MODEL = False

# score_total に対するベース勝率
SCORE_BASE_PROB = {
    3: 0.52,
    4: 0.55,
    5: 0.58,
    6: 0.62,
    7: 0.66,
    8: 0.70,
    9: 0.73,
    10: 0.76,
}

# 強い肯定理由（ボーナス）
STRONG_REASON_BONUS = {
    # BUY
    "bull_big_combo": 0.10,
    "gap_up_breakout": 0.08,
    "perfect_order_event": 0.06,
    "breakout_high": 0.05,

    # SELL
    "bear_big_combo": 0.10,
    "gap_down_breakdown": 0.08,
    "perfect_order_down": 0.06,
}

# 危険シグナル（ペナルティ）
RISK_REASON_PENALTY = {
    "rsi_overbought_70": -0.05,
    "bb_upper_touch": -0.05,
    "volume_drop": -0.04,
    "ma_reversal_after_touch": -0.04,
}

# 確率の上限（過信防止）
MAX_PROBABILITY = 0.95


# ============================================================
# メイン API
# ============================================================

def estimate_entry_probability(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    ENTRY 勝率を推定する（※可否判断は entry_gate が担当）

    Returns:
        {
            "probability": float (0.0 - 1.0),
            "confidence": "LOW" | "MID" | "HIGH",   # 確率の強さラベル
            "reasons": list[str],                   # 人間向け説明
            "method": "HEURISTIC" | "ML"
        }
    """

    if not isinstance(row, dict):
        return _empty_result("invalid_row")

    symbol = str(row.get("symbol") or "")
    score_total = int(row.get("score_total") or 0)
    reasons = row.get("score_reasons") or {}

    if not isinstance(reasons, dict):
        reasons = {}

    debug_reasons: List[str] = []

    # ========================================================
    # ① ベース確率（score_total）
    # ========================================================
    prob = _base_probability(score_total)
    debug_reasons.append(f"base(score={score_total})")

    # ========================================================
    # ② 強化要因
    # ========================================================
    for r in reasons.keys():
        if r in STRONG_REASON_BONUS:
            delta = STRONG_REASON_BONUS[r]
            prob += delta
            debug_reasons.append(f"+{r}({delta:+.2f})")

    # ========================================================
    # ③ リスク要因
    # ========================================================
    for r in reasons.keys():
        if r in RISK_REASON_PENALTY:
            delta = RISK_REASON_PENALTY[r]
            prob += delta
            debug_reasons.append(f"{r}({delta:+.2f})")

    # ========================================================
    # ④ マーケット状態（軽量）
    # ========================================================
    market_state = row.get("market_state")
    if market_state == "RISK_OFF":
        prob -= 0.05
        debug_reasons.append("market=RISK_OFF(-0.05)")

    # ========================================================
    # ⑤ ML モデル（将来用 / 任意）
    # ========================================================
    method = "HEURISTIC"

    if USE_ML_MODEL:
        try:
            from AI.train.entry_probability_ml import predict_probability_ml

            ml_prob = predict_probability_ml(row)
            if isinstance(ml_prob, (int, float)):
                prob = float(ml_prob)
                method = "ML"
                debug_reasons.append("ML_OVERRIDE")

        except Exception as e:
            logger.warning(
                "ENTRY_PROB ML failed → heuristic fallback (%s)", e
            )

    # ========================================================
    # clamp & label
    # ========================================================
    prob = max(0.0, min(prob, MAX_PROBABILITY))
    confidence = _confidence_label(prob)

    logger.debug(
        "ENTRY_PROB symbol=%s score=%d prob=%.3f conf=%s method=%s",
        symbol,
        score_total,
        prob,
        confidence,
        method,
    )

    return {
        "probability": round(prob, 3),
        "confidence": confidence,
        "reasons": debug_reasons,
        "method": method,
    }


# ============================================================
# 内部関数
# ============================================================

def _base_probability(score: int) -> float:
    """
    score_total からベース勝率を算出
    """
    if score <= 2:
        return 0.45

    for s in sorted(SCORE_BASE_PROB.keys(), reverse=True):
        if score >= s:
            return SCORE_BASE_PROB[s]

    return 0.50


def _confidence_label(prob: float) -> str:
    """
    人間向け確率ラベル
    """
    if prob >= 0.70:
        return "HIGH"
    if prob >= 0.60:# ============================================================
# AI/entry_probability.py
# ------------------------------------------------------------
# ✔ スコア・理由・市場状態から ENTRY 勝率を推定
# ✔ 既存ルール（score_config.ini）を一切破壊しない
# ✔ entry_gate 用「確率」だけを返す
# ✔ 将来 ML / NN（model_*.pkl）に差し替え可能
# ============================================================

import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# ============================================================
# 🔧 設定
# ============================================================

# ML を使うか（False = 完全ヒューリスティック）
USE_ML_MODEL = False

# score_total に対するベース勝率
SCORE_BASE_PROB = {
    3: 0.52,
    4: 0.55,
    5: 0.58,
    6: 0.62,
    7: 0.66,
    8: 0.70,
    9: 0.73,
    10: 0.76,
}

# 強い肯定理由（ボーナス）
STRONG_REASON_BONUS = {
    # BUY
    "bull_big_combo": 0.10,
    "gap_up_breakout": 0.08,
    "perfect_order_event": 0.06,
    "breakout_high": 0.05,

    # SELL
    "bear_big_combo": 0.10,
    "gap_down_breakdown": 0.08,
    "perfect_order_down": 0.06,
}

# 危険シグナル（ペナルティ）
RISK_REASON_PENALTY = {
    "rsi_overbought_70": -0.05,
    "bb_upper_touch": -0.05,
    "volume_drop": -0.04,
    "ma_reversal_after_touch": -0.04,
}

# 確率の上限（過信防止）
MAX_PROBABILITY = 0.95


# ============================================================
# メイン API
# ============================================================

def estimate_entry_probability(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    ENTRY 勝率を推定する（最終判断は entry_gate が行う）

    Returns:
        {
            "probability": float (0.0 - 1.0),
            "confidence": "LOW" | "MID" | "HIGH",
            "reasons": list[str],
            "method": "HEURISTIC" | "ML"
        }
    """

    if not isinstance(row, dict):
        return _empty_result("invalid_row")

    symbol = row.get("symbol", "")
    score_total = int(row.get("score_total", 0) or 0)
    reasons: Dict[str, int] = row.get("score_reasons", {}) or {}

    debug_reasons: List[str] = []

    # ========================================================
    # ① ベース確率（score_total）
    # ========================================================
    prob = _base_probability(score_total)
    debug_reasons.append(f"base(score={score_total})")

    # ========================================================
    # ② 強化要因
    # ========================================================
    for r in reasons.keys():
        if r in STRONG_REASON_BONUS:
            delta = STRONG_REASON_BONUS[r]
            prob += delta
            debug_reasons.append(f"+{r}({delta:+.2f})")

    # ========================================================
    # ③ リスク要因
    # ========================================================
    for r in reasons.keys():
        if r in RISK_REASON_PENALTY:
            delta = RISK_REASON_PENALTY[r]
            prob += delta
            debug_reasons.append(f"{r}({delta:+.2f})")

    # ========================================================
    # ④ マーケット状態
    # ========================================================
    market_state = row.get("market_state")
    if market_state == "RISK_OFF":
        prob -= 0.05
        debug_reasons.append("market=RISK_OFF(-0.05)")

    # ========================================================
    # ⑤ ML モデル（将来用 / OFF 可能）
    # ========================================================
    method = "HEURISTIC"

    if USE_ML_MODEL:
        try:
            from AI.train.entry_probability_ml import predict_probability_ml
            ml_prob = predict_probability_ml(row)
            if isinstance(ml_prob, (int, float)):
                prob = float(ml_prob)
                method = "ML"
                debug_reasons.append("ML_OVERRIDE")
        except Exception as e:
            logger.warning("ML probability failed → fallback heuristic: %s", e)

    # ========================================================
    # clamp & label
    # ========================================================
    prob = max(0.0, min(prob, MAX_PROBABILITY))
    confidence = _confidence_label(prob)

    logger.debug(
        "ENTRY_PROB symbol=%s score=%d prob=%.3f conf=%s method=%s",
        symbol,
        score_total,
        prob,
        confidence,
        method,
    )

    return {
        "probability": round(prob, 3),
        "confidence": confidence,
        "reasons": debug_reasons,
        "method": method,
    }


# ============================================================
# 内部関数
# ============================================================

def _base_probability(score: int) -> float:
    """
    score_total からベース勝率を算出
    """
    if score <= 2:
        return 0.45

    for s in sorted(SCORE_BASE_PROB.keys(), reverse=True):
        if score >= s:
            return SCORE_BASE_PROB[s]

    return 0.50


def _confidence_label(prob: float) -> str:
    if prob >= 0.70:
        return "HIGH"
    if prob >= 0.60:
        return "MID"
    return "LOW"


def _empty_result(reason: str) -> Dict[str, Any]:
    return {
        "probability": 0.0,
        "confidence": "LOW",
        "reasons": [reason],
        "method": "NONE",
    }

        return "MID"
    return "LOW"


def _empty_result(reason: str) -> Dict[str, Any]:
    return {
        "probability": 0.0,
        "confidence": "LOW",
        "reasons": [reason],
        "method": "NONE",
    }

