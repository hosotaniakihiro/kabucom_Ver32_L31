# ============================================================
# AI/train_lgbm_by_timeframe.py
# ------------------------------------------------------------
# ✔ 時間足ごとに LightGBM 学習
# ✔ 回帰モデル
# ✔ 銘柄混合
# ✔ model_xx.pkl を自動生成
# ============================================================

import pandas as pd
from pathlib import Path
import joblib
import lightgbm as lgb
from sklearn.model_selection import train_test_split

TRAIN_DIR = Path("AI/train")
MODEL_DIR = Path("AI/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# 学習対象カラム
# ============================================================
FEATURE_COLS = [
    "ret",
    "body",
    "range",
    "vol_ratio",
    "fast_ret",
    "symbol_id",
]

TARGET_COL = "y"


# ============================================================
# 学習処理
# ============================================================
def train_one(csv_path: Path):
    timeframe = csv_path.stem.replace("train_", "")
    print(f"\n🚀 Training {timeframe}")

    df = pd.read_csv(csv_path)

    X = df[FEATURE_COLS]
    y = df[TARGET_COL]

    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, shuffle=False
    )

    model = lgb.LGBMRegressor(
        objective="regression",
        num_leaves=64,
        learning_rate=0.05,
        n_estimators=400,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
    )

    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="l2",
        verbose=50,
    )

    out_path = MODEL_DIR / f"model_{timeframe}.pkl"
    joblib.dump(model, out_path)

    print(f"✅ saved {out_path}")


# ============================================================
# メイン
# ============================================================
def main():
    for csv_path in TRAIN_DIR.glob("train_*.csv"):
        train_one(csv_path)


if __name__ == "__main__":
    main()
