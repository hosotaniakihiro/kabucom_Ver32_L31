# ============================================================
# AI/train_lgbm_retrain.py
# Ver24-FINAL-RETRAIN
# ------------------------------------------------------------
# ✔ 実トレード結果から再学習
# ✔ sklearn 不要
# ✔ LightGBM callbacks 方式（後方互換）
# ✔ 勝ち / 負け（二値分類）
# ============================================================

import lightgbm as lgb
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

# ============================================================
# 設定
# ============================================================

DATA_PATH = Path("AI/train_data_real.csv")
MODEL_OUT = Path("AI/lgbm_retrained_model.txt")

TARGET_COL = "y"

FEATURE_COLS = [
    "summary_score",
    "ranking_score",
    "ai_prob",
    "final_score",
    "interval",
]

# ============================================================
# util
# ============================================================

def train_valid_split(df, valid_ratio=0.2):
    """
    時系列順を保ったまま train / valid に分割
    """
    n = len(df)
    split = int(n * (1 - valid_ratio))
    train_df = df.iloc[:split]
    valid_df = df.iloc[split:]
    return train_df, valid_df


# ============================================================
# main
# ============================================================

def main():
    print("📥 学習データ読み込み")

    if not DATA_PATH.exists():
        raise FileNotFoundError(DATA_PATH)

    df = pd.read_csv(DATA_PATH)

    # ------------------------------
    # 前処理
    # ------------------------------
    df = df.dropna(subset=[TARGET_COL])
    df = df.fillna(0)

    print(f"📊 学習行数: {len(df)}")

    X = df[FEATURE_COLS]
    y = df[TARGET_COL].astype(int)

    # ------------------------------
    # train / valid 分割
    # ------------------------------
    train_df, valid_df = train_valid_split(df)

    X_train = train_df[FEATURE_COLS]
    y_train = train_df[TARGET_COL].astype(int)

    X_valid = valid_df[FEATURE_COLS]
    y_valid = valid_df[TARGET_COL].astype(int)

    train_ds = lgb.Dataset(X_train, label=y_train)
    valid_ds = lgb.Dataset(X_valid, label=y_valid)

    # ------------------------------
    # パラメータ（実戦向け）
    # ------------------------------
    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": -1,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbosity": -1,
        "seed": 42,
    }

    print("🚀 学習開始")

    model = lgb.train(
        params,
        train_ds,
        num_boost_round=500,
        valid_sets=[valid_ds],
        callbacks=[
            lgb.early_stopping(stopping_rounds=30),
            lgb.log_evaluation(period=50),
        ],
    )

    # ------------------------------
    # 評価
    # ------------------------------
    best_iter = model.best_iteration
    pred = model.predict(X_valid, num_iteration=best_iter)

    # AUC 手計算（sklearn 不使用）
    def auc_score(y_true, y_score):
        order = np.argsort(y_score)
        y_true = y_true.values[order]
        cum_pos = np.cumsum(y_true)
        total_pos = cum_pos[-1]
        total_neg = len(y_true) - total_pos
        if total_pos == 0 or total_neg == 0:
            return 0.0
        auc = (cum_pos[y_true == 0].sum() - total_neg * (total_neg + 1) / 2) / (
            total_pos * total_neg
        )
        return auc

    auc = auc_score(y_valid, pred)

    print(f"🎯 AUC: {auc:.4f}")
    print(f"⭐ best_iteration: {best_iter}")

    # ------------------------------
    # モデル保存
    # ------------------------------
    model.save_model(str(MODEL_OUT))
    print(f"💾 モデル保存: {MODEL_OUT}")

    # ------------------------------
    # 特徴量重要度
    # ------------------------------
    print("\n📊 Feature Importance")
    imp = model.feature_importance()
    for name, score in sorted(
        zip(FEATURE_COLS, imp), key=lambda x: x[1], reverse=True
    ):
        print(f"{name:15s} : {score}")

    print("✅ 再学習完了")


if __name__ == "__main__":
    main()
