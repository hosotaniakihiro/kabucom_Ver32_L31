# ============================================================
# File   : AI/tools/shap_exit_holdtime.py
# Ver1.0-FINAL-SHAP-EXIT-HOLDTIME
# ------------------------------------------------------------
# ✔ EXITホールド時間AI（回帰）のSHAP可視化
# ✔ 「なぜ長く / 短く持つべきか」を説明
# ============================================================

import pickle
from pathlib import Path
import pandas as pd
import shap
import matplotlib.pyplot as plt

# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = PROJECT_ROOT / "AI" / "models" / "exit_holdtime_lgbm.pkl"
DB_PATH = PROJECT_ROOT / "AI" / "data" / "ai_entry_events.db"

# ============================================================
# LOAD MODEL
# ============================================================

with open(MODEL_PATH, "rb") as f:
    obj = pickle.load(f)

model = obj["model"]
FEATURES = obj["features"]

explainer = shap.TreeExplainer(model)

# ============================================================
# LOAD DATA
# ============================================================

def load_sample(n=500):
    import sqlite3

    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        f"""
        SELECT {",".join(FEATURES)}
        FROM entry_events
        WHERE exit_time IS NOT NULL
        ORDER BY id DESC
        LIMIT {n}
        """,
        con,
    )
    con.close()
    return df.dropna()


# ============================================================
# SHAP PLOT
# ============================================================

def main():
    df = load_sample()
    shap_values = explainer.shap_values(df[FEATURES])

    shap.summary_plot(
        shap_values,
        df[FEATURES],
        show=False,
    )
    plt.title("EXIT HoldTime SHAP")
    plt.show()


if __name__ == "__main__":
    main()
