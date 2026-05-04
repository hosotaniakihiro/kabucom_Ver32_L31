# pj/AI/infer/tonosama_infer.py
import json, joblib
from global_state import global_data

MODEL = joblib.load("AI/models/tonosama_lgbm.pkl")
FEATURES = json.load(open("AI/models/tonosama_features.json"))
TH = json.load(open("AI/models/tonosama_thresholds.json"))

def infer_tonosama(symbol, features: dict):
    x = [features.get(f, 0) for f in FEATURES]
    prob = MODEL.predict([x])[0]

    hour = features.get("entry_second", 0) // 3600
    sym_th = TH.get(symbol, {}).get(str(hour), {})

    if prob < sym_th.get("ai_conf", 0.6):
        return {"ok": False, "ai_confidence": prob}

    if features["fast_ret"] < sym_th.get("fast_ret", 0.2):
        return {"ok": False, "ai_confidence": prob}

    return {"ok": True, "ai_confidence": prob}
