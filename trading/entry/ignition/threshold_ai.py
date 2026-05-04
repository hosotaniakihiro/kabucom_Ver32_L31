threshold_ai.pyzimport pandas as pd
from pathlib import Path
import joblib

DATA_PATH = Path("AI/train/tosama_train.csv")
OUT_PATH = Path("AI/model/tonosama_thresholds.pkl")


def build_thresholds():
    df = pd.read_csv(DATA_PATH)

    thresholds = {}

    for symbol, g in df.groupby("symbol"):
        if len(g) < 30:
            continue

        # 勝率最大化する ai_conf 閾値
        best_th = 0.72
        best_score = 0

        for th in [0.65, 0.68, 0.70, 0.72, 0.75]:
            win = g[g["ai_confidence"] >= th]
            if len(win) < 5:
                continue
            score = win["label"].mean()
            if score > best_score:
                best_score = score
                best_th = th

        thresholds[symbol] = best_th

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(thresholds, OUT_PATH)
    print(f"saved -> {OUT_PATH}")


if __name__ == "__main__":
    build_thresholds()
