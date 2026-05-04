# ============================================================
# AI/train/sell/sell_lgbm_stop.py
# SELL STOP 判定（推論専用・軽量）
# ============================================================

from pathlib import Path
import joblib
import pandas as pd

# ============================================================
MODEL_PATH = Path("AI/model/sell_lgbm_stop.pkl")
_model = None

# ============================================================
def _load():
    global _model
    if _model is None and MODEL_PATH.exists():
        _model = joblib.load(MODEL_PATH)
    return _model

# ============================================================
def predict_sell_stop(features: dict, threshold: float = 0.45) -> bool:
    """
    STOP してよいか？

    Returns:
        bool
    """

    model = _load()
    if model is None:
        return False  # 安全側

    X = pd.DataFrame([{
        "drawdown_rate": features.get("drawdown_rate", 0),
        "hold_seconds": features.get("hold_seconds", 0),
        "volume_speed": features.get("volume_speed", 0),
        "volatility": features.get("volatility", 0),
    }])

    prob = float(model.predict_proba(X)[0, 1])
    return prob >= threshold
