# trading/entry/ai_immediate_profit.py

import joblib
from pathlib import Path

MODEL = joblib.load(Path("AI/model_entry_immediate_profit.pkl"))

FEATURES = [
    "volume_speed",
    "price_velocity",
    "spread",
    "distance_from_vwap",
    "breakout_strength",
    "orderbook_imbalance",
]

def predict_immediate_profit(feature_dict):
    X = [[feature_dict[f] for f in FEATURES]]
    return float(MODEL.predict_proba(X)[0][1])
