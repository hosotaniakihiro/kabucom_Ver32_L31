# ============================================================
# AI/optuna/optimize_ai_weights.py
# ------------------------------------------------------------
# ✔ 過去トレードからAI重み最適化
# ✔ ロジック不変・重みのみ
# ============================================================

import optuna
import numpy as np
import pandas as pd

ENTRY_TH = 10.0  # 実運用と同じ

def load_trade_logs() -> pd.DataFrame:
    # ← あなたのDB or CSVに合わせて実装
    return pd.read_csv("trade_logs.csv")


def objective(trial):
    w_1m  = trial.suggest_float("w_1M",  0.5, 3.0)
    w_2m  = trial.suggest_float("w_2M",  0.0, 2.0)
    w_10s = trial.suggest_float("w_10S", 0.0, 1.5)

    pnl_list = []

    df = load_trade_logs()

    for _, r in df.iterrows():
        score = r["base_score"]

        score += w_1m  if r["ai_1m"]  > 0 else -w_1m
        score += w_2m  if r["ai_2m"]  > 0 else -w_2m
        score += w_10s if r["ai_10s"] > 0 else -w_10s

        if score >= ENTRY_TH:
            pnl_list.append(r["pnl"])

    if not pnl_list:
        return -1e6

    return np.mean(pnl_list)


def main():
    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=100)

    print("=== BEST ===")
    print(study.best_params)
    print("mean pnl:", study.best_value)


if __name__ == "__main__":
    main()
