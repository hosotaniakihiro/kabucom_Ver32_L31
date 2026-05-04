# AI/config_loader.py
import json

def load_timeband_threshold():
    try:
        with open("AI/config/timeband_ai_threshold.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}
