# ============================================================
# AI/inference/entry_ai.py
# ------------------------------------------------------------
# ENTRY 成否推論（安全ラッパー）
# ============================================================

import logging
from AI.inference.model_loader import load_model

logger = logging.getLogger(__name__)

FEATURE_COLS = [
    "ret",
    "body",
    "range",
    "vol_ratio",
    "fast_ret",
]

def predict_entry_prob(features: dict) -> float | None:
    """
    ENTRY 成功確率を返す
    失敗時は None（Runtime 安全）
    """
    try:
        model = load_model("ENTRY")
        if model is None:
            return None

        X = [[
            float(features.get(c, 0.0))
            for c in FEATURE_COLS
        ]]

        return float(model.predict_proba(X)[0, 1])

    except Exception:
        logger.exception("[ENTRY_AI_PRED_ERROR]")
        return None
