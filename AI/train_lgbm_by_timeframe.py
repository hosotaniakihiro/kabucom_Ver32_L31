# ============================================================
# AI/train_lgbm_by_timeframe.py
# ------------------------------------------------------------
# ✔ 巨大CSV対応 LightGBM 学習
# ✔ chunk 読み込み（メモリ非依存）
# ✔ Validation（RMSE）追加
# ✔ 時間足ごとに model_*.pkl 作成
# ============================================================

import gc
from pathlib import Path

import pandas as pd
import lightgbm as lgb
import joblib
from sklearn.metrics import mean_squared_error

from math import sqrt

# ============================================================
# パス設定
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent
TRAIN_DIR = SCRIPT_DIR / "train"
MODEL_DIR = SCRIPT_DIR / "models"
MODEL_DIR.mkdir(exist_ok=True)

# ============================================================
# 学習設定
# ============================================================

FEATURE_COLS = [
    "ret",
    "body",
    "range",
    "vol_ratio",
    "fast_ret",
]

TARGET_COL = "y"

CHUNK_SIZE = 500_000
USE_COLS = FEATURE_COLS + [TARGET_COL]

# Validation 用に最後の N 行を保持
VALIDATION_ROWS = 200_000

# ============================================================
# LightGBM パラメータ
# ============================================================
LGB_PARAMS = dict(
    objective="regression",
    num_leaves=64,
    learning_rate=0.05,
    n_estimators=300,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=42,
    force_row_wise=True,
    n_jobs=-1,
)

# ============================================================
# 時間足ごとの学習
# ============================================================
def train_one(csv_path: Path):
    timeframe = csv_path.stem.replace("train_", "")
    print(f"\n🚀 Training {timeframe}")

    model = lgb.LGBMRegressor(**LGB_PARAMS)

    first_fit = True
    total_rows = 0

    # validation 用バッファ
    val_buf = []

    for chunk in pd.read_csv(
        csv_path,
        chunksize=CHUNK_SIZE,
        usecols=USE_COLS,
    ):
        chunk = chunk.dropna()
        if chunk.empty:
            continue

        # validation 用に後ろから確保
        if len(val_buf) < VALIDATION_ROWS:
            val_buf.append(chunk.tail(VALIDATION_ROWS // 4))
            chunk = chunk.iloc[:-len(chunk.tail(VALIDATION_ROWS // 4))]

        if chunk.empty:
            continue

        X = chunk[FEATURE_COLS]
        y = chunk[TARGET_COL]

        if first_fit:
            model.fit(X, y)
            first_fit = False
        else:
            model.fit(X, y, init_model=model)

        total_rows += len(chunk)

        del chunk, X, y
        gc.collect()

    if first_fit:
        print(f"⚠ no valid data: {csv_path.name}")
        return

    # ========================================================
    # Validation 評価
    # ========================================================
    val_df = pd.concat(val_buf).dropna()
    X_val = val_df[FEATURE_COLS]
    y_val = val_df[TARGET_COL]

    y_pred = model.predict(X_val)
    mse = mean_squared_error(y_val, y_pred)
    rmse = sqrt(mse)

    print(f"📊 Validation RMSE [{timeframe}]: {rmse:.6f}")

    # ========================================================
    # モデル保存
    # ========================================================
    out_path = MODEL_DIR / f"model_{timeframe}.pkl"
    joblib.dump(model, out_path)

    print(f"✅ saved {out_path} (rows={total_rows:,})")

    del val_df, X_val, y_val, y_pred
    gc.collect()


# ============================================================
# エントリーポイント
# ============================================================
def main():
    csv_files = sorted(TRAIN_DIR.glob("train_*.csv"))

    if not csv_files:
        print("❌ train_*.csv が見つかりません")
        return

    for csv_path in csv_files:
        train_one(csv_path)


if __name__ == "__main__":
    main()
