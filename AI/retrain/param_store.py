# ============================================================
# AI/retrain/param_store.py
# ============================================================

import json
import os
import datetime as dt

PARAM_PATH = "AI/config/risk_ai_params.json"
HISTORY_PATH = "logs/retrain_history.csv"


def load_params() -> dict:
    if not os.path.exists(PARAM_PATH):
        return {}
    with open(PARAM_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_params(params: dict, score: float):
    os.makedirs(os.path.dirname(PARAM_PATH), exist_ok=True)

    data = {
        "params": params,
        "score": score,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }

    with open(PARAM_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # --- 履歴 ---
    header = not os.path.exists(HISTORY_PATH)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        if header:
            f.write("datetime,score,params\n")
        f.write(
            f"{data['updated_at']},{score},{json.dumps(params)}\n"
        )
