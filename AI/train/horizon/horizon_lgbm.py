# ============================================================
# horizon_lgbm.py
# horizon EXIT 推論
# ============================================================

from pathlib import Path
import joblib
import pandas as pd

MODEL_PATH = Path("AI/model/horizon_lgbm.pkl")
_model = None

def _load():
    global _model
    if _model is None and MODEL_PATH.exists():
        _model = joblib.load(MODEL_PATH)
    return _model

def predict_best_horizon(features: dict) -> int:
    """
    Returns:
        int: 推奨 horizon 秒（30 / 60 / 120 / 0）
    """
    model = _load()
    if model is None:
        return 0

    X = pd.DataFrame([{
        "profit_rate": float(features.get("profit_rate", 0)),
    }])

    try:
        probs = model.predict_proba(X)
        scores = {
            30: probs[0][0],
            60: probs[0][1],
            120: probs[0][2],
        }
        return max(scores, key=scores.get)
    except Exception:
        return 0
