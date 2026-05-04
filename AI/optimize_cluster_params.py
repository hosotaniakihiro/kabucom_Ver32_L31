# ============================================================
# pj/ai/optimize_cluster_params.py
# Optuna によるクラスタ別パラメータ自動探索
# ============================================================

import optuna
import pandas as pd
import joblib
from pathlib import Path

DATA_PATH = Path("AI/train/tosama_train.csv")
SAVE_PATH = Path("AI/model/cluster_params_optuna.pkl")

CLUSTER_COL = "cluster_id"
TARGET = "label"

FEATURES = [
    "volume_speed",
    "fast_ret",
    "ai_confidence",
]


def evaluate(df, vol_th, fast_ret_th, ai_th):
    cond = (
        (df["volume_speed"] >= vol_th) &
        (df["fast_ret"] >= fast_ret_th) &
        (df["ai_confidence"] >= ai_th)
    )
    trades = df[cond]
    if len(trades) < 20:
        return -1.0

    win_rate = trades[TARGET].mean()
    pnl = trades["pnl_pct"].mean()
    return win_rate * pnl * 100


def optimize_cluster(cluster_df):

    def objective(trial):
        vol_th = trial.suggest_int("volume_speed", 3000, 12000)
        fast_ret_th = trial.suggest_float("fast_ret", 0.05, 0.5)
        ai_th = trial.suggest_float("ai_confidence", 0.55, 0.9)

        score = evaluate(cluster_df, vol_th, fast_ret_th, ai_th)
        return score

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=80)
    return study.best_params


def main():
    df = pd.read_csv(DATA_PATH)
    params = {}

    for cid in sorted(df[CLUSTER_COL].unique()):
        cdf = df[df[CLUSTER_COL] == cid]
        if len(cdf) < 50:
            continue

        best = optimize_cluster(cdf)
        params[int(cid)] = best
        print(f"cluster {cid} best = {best}")

    SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(params, SAVE_PATH)
    print("✅ cluster params optimized")


if __name__ == "__main__":
    main()
