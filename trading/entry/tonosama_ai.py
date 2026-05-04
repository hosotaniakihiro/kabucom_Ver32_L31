# trading/entry/tonosama_ai.py
import joblib
import pandas as pd

_model = None

def load_model():
    global _model
    if _model is None:
        _model = joblib.load("AI/tonosama_model.pkl")
    return _model


def tonosama_judge(features: dict) -> float:
    model = load_model()
    df = pd.DataFrame([features])
    prob = model.predict_proba(df)[0, 1]
    return prob
