# ============================================================
# AI/decision/final_decision_ai.py
# FINAL DECISION AI（EXIT最終判断）
# Ver1.2-SAFE
# ------------------------------------------------------------
# ✔ EXITを強制しない（拒否権のみ）
# ✔ 学習CSV / 学習コードと完全一致
# ✔ LOG → weak gate → control 昇格対応
# ✔ 異常時は必ず GO（安全側）
# ============================================================

import logging
from pathlib import Path
import joblib

logger = logging.getLogger("FINAL_DECISION_AI")

# ============================================================
# MODEL PATH
# ============================================================

MODEL_PATH = Path("AI/model/final_decision_lgbm.pkl")

_model = None

# ============================================================
# FEATURES（train_final_decision_lgbm.py と完全一致）
# ============================================================

FEATURE_ORDER = [
    "profit_rate",
    "drawdown_rate",
    "hold_seconds",
    "volume_speed",
    "volatility",
    "trend_strength",
]

# ラベル定義
LABEL_DELAY = 0
LABEL_HOLD  = 1
LABEL_GO    = 2


# ============================================================
# モデルロード（遅延）
# ============================================================

def _load_model():
    global _model
    if _model is not None:
        return _model

    if not MODEL_PATH.exists():
        logger.warning("[FINAL_AI] model not found")
        return None

    try:
        _model = joblib.load(MODEL_PATH)
        logger.info("[FINAL_AI] model loaded")
        return _model
    except Exception:
        logger.exception("[FINAL_AI] model load failed")
        _model = None
        return None


# ============================================================
# FINAL DECISION
# ============================================================

def infer_final_decision(features: dict) -> dict:
    """
    FINAL AI による EXIT 最終判断（拒否権）

    Returns
    -------
    dict
        {
          "decision": "GO" | "HOLD" | "DELAY",
          "confidence": float,
          "next_check_sec": int
        }
    """

    # --------------------------------------------------------
    # 安全ガード
    # --------------------------------------------------------
    if not features:
        return {
            "decision": "GO",
            "confidence": 0.0,
            "next_check_sec": 0,
        }

    model = _load_model()
    if model is None:
        return {
            "decision": "GO",
            "confidence": 0.0,
            "next_check_sec": 0,
        }

    try:
        # ----------------------------------------------------
        # 特徴量整形（完全一致）
        # ----------------------------------------------------
        X = [[float(features.get(k, 0.0)) for k in FEATURE_ORDER]]

        pred = int(model.predict(X)[0])

        prob = None
        if hasattr(model, "predict_proba"):
            prob = model.predict_proba(X)[0]

        # ----------------------------------------------------
        # 判定
        # ----------------------------------------------------
        if pred == LABEL_HOLD:
            return {
                "decision": "HOLD",
                "confidence": float(prob[LABEL_HOLD]) if prob is not None else 0.6,
                "next_check_sec": 5,
            }

        if pred == LABEL_DELAY:
            return {
                "decision": "DELAY",
                "confidence": float(prob[LABEL_DELAY]) if prob is not None else 0.5,
                "next_check_sec": 10,
            }

        # LABEL_GO
        return {
            "decision": "GO",
            "confidence": float(prob[LABEL_GO]) if prob is not None else 0.7,
            "next_check_sec": 0,
        }

    except Exception:
        logger.exception("[FINAL_AI] inference error")
        return {
            "decision": "GO",
            "confidence": 0.0,
            "next_check_sec": 0,
        }
