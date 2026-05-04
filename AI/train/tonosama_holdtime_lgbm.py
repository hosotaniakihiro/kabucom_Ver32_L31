# ============================================================
# AI/train/tonosama_holdtime_lgbm.py
# 殿様イナゴ 最適 holding 秒数 LightGBM 回帰学習
# Updated: 2025-12-31
# ------------------------------------------------------------
# ✔ CSV → holding 秒数 回帰学習
# ✔ MAE / RMSE で評価
# ✔ holdtime_ai.py から predict 利用
# ============================================================

import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path

from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error

# ============================================================
# パス設定
# ============================================================
DATA_PATH  = Path("AI/train/tosama_train.csv")
MODEL_PATH = Path("AI/model/tonosama_holdtime_lgbm.pkl")

# ============================================================
# 特徴量（holdtime_ai.py と完全一致）
# ============================================================
FEATURES = [
    "volume_speed",
    "fast_ret",
    "rank_position",
    "price",
    "spread",
    "entry_second",
]

TARGET = "hold_seconds"


# ============================================================
# メイン
# ============================================================
def main():

    if not DATA_PATH.exists():
        raise FileNotFoundError(f"❌ 学習CSVが存在しません: {DATA_PATH}")

    df = pd.read_csv(DATA_PATH)

    # -----------------------------------------
    # 前処理
    # -----------------------------------------
    df = df.dropna(subset=FEATURES + [TARGET])

    # 異常値ガード（0～300秒に制限）
    df = df[(df[TARGET] >= 5) & (df[TARGET] <= 300)]

    X = df[FEATURES]
    y = df[TARGET]

    if len(df) < 200:
        raise ValueError("❌ 学習データが少なすぎます（200件以上推奨）")

    # -----------------------------------------
    # train / validation split
    # -----------------------------------------
    X_train, X_val, y_train, y_val = train_test_split(
        X,
        y,
        test_size=0.2,
        random_state=42,
    )

    # -----------------------------------------
    # LightGBM 回帰モデル
    # -----------------------------------------
    model = lgb.LGBMRegressor(
        objective="regression",
        boosting_type="gbdt",
        n_estimators=500,
        learning_rate=0.03,
        max_depth=6,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
    )

    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="rmse",
        verbose=False,
    )

    # -----------------------------------------
    # 評価
    # -----------------------------------------
    pred = model.predict(X_val)

    mae  = mean_absolute_error(y_val, pred)
    rmse = mean_squared_error(y_val, pred, squared=False)

    print("=" * 60)
    print(f"⏱️ HOLDTIME MODEL  MAE  = {mae:.2f} sec")
    print(f"⏱️ HOLDTIME MODEL  RMSE = {rmse:.2f} sec")
    print("=" * 60)

    # -----------------------------------------
    # 保存
    # -----------------------------------------
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    print(f"💾 saved -> {MODEL_PATH}")


# ============================================================
# 実行
# ============================================================
if __name__ == "__main__":
    main()
