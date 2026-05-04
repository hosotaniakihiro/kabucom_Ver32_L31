import pandas as pd
import shap
import joblib
from pathlib import Path

MODEL_PATH = Path("AI/model/tonosama_entry_lgbm.pkl")
DATA_PATH  = Path("AI/train/tosama_train.csv")

FEATURES = [
    "volume_speed",
    "fast_ret",
    "rank_position",
    "price",
    "spread",
    "entry_second",
]

def main():
    model = joblib.load(MODEL_PATH)
    df = pd.read_csv(DATA_PATH).dropna(subset=FEATURES)

    # entry_second 正規化（学習と同じ）
    df["entry_second"] = df["entry_second"] / 59.0
    X = df[FEATURES]

    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X)

    # 重要度一覧
    shap.summary_plot(
        shap_values[1],
        X,
        plot_type="bar"
    )

    # 分布
    shap.summary_plot(
        shap_values[1],
        X
    )

if __name__ == "__main__":
    main()
