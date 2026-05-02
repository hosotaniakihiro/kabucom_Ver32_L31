# ============================================================
# File: AI/tonosama_train_lgbm.py
# ------------------------------------------------------------
# 殿様イナゴ（BUY）専用 LightGBM 学習スクリプト
#
# ✔ 目的：60秒以内に +0.5% 到達するか（分類）
# ✔ 単一モデル・軽量
# ✔ 過学習抑制重視
# ✔ 日次再学習対応
# ✔ 学習 / 検証 / 本番 共通
# ============================================================

from __future__ import annotations

import os
import lightgbm as lgb
import pandas as pd
from sklearn.model_selection import train_test_split


# ============================================================
# 設定
# ============================================================

TRAIN_CSV = os.environ.get(
    "TONOSAMA_TRAIN_CSV",
    "tonosama_train.csv"
)

MODEL_PATH = os.environ.get(
    "TONOSAMA_MODEL_PATH",
    "tonosama_lgbm.txt"
)

FEATURES = [
    "price_velocity",
    "volume_speed",
    "rank_jump",
    "rank_strength",
    "dominant_ratio",
    "spread_ratio",
    "minute_from_open",
]

RANDOM_STATE = 42


# ============================================================
# メイン学習処理
# ============================================================

def train_model() -> lgb.Booster:
    """
    殿様イナゴ BUY 用 LightGBM モデルを学習する
    """

    # --------------------------------------------------------
    # データ読み込み
    # --------------------------------------------------------
    if not os.path.exists(TRAIN_CSV):
        raise FileNotFoundError(f"train csv not found: {TRAIN_CSV}")

    df = pd.read_csv(TRAIN_CSV)

    # 必須カラムチェック
    missing = [c for c in FEATURES + ["label"] if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")

    # NaN / inf 除去（安全第一）
    df = df.replace([float("inf"), float("-inf")], 0.0)
    df = df.fillna(0.0)

    X = df[FEATURES]
    y = df["label"].astype(int)

    # --------------------------------------------------------
    # train / validation split
    # --------------------------------------------------------
    X_train, X_val, y_train, y_val = train_test_split(
        X,
        y,
        test_size=0.2,
        shuffle=True,
        random_state=RANDOM_STATE,
        stratify=y if y.nunique() > 1 else None,
    )

    train_ds = lgb.Dataset(X_train, label=y_train)
    val_ds   = lgb.Dataset(X_val, label=y_val)

    # --------------------------------------------------------
    # パラメータ（殿様専用・固定）
    # --------------------------------------------------------
    params = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",

        # 学習挙動
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": -1,
        "min_data_in_leaf": 50,

        # 過学習抑制
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,

        # 安定性
        "lambda_l1": 0.0,
        "lambda_l2": 0.0,

        # ログ
        "verbosity": -1,
        "seed": RANDOM_STATE,
    }

    # --------------------------------------------------------
    # 学習
    # --------------------------------------------------------
    model = lgb.train(
        params,
        train_ds,
        valid_sets=[train_ds, val_ds],
        valid_names=["train", "valid"],
        num_boost_round=500,
        early_stopping_rounds=50,
    )

    # --------------------------------------------------------
    # 保存
    # --------------------------------------------------------
    model.save_model(MODEL_PATH)

    return model


# ============================================================
# エントリーポイント
# ============================================================

def main():
    model = train_model()

    # 学習結果の簡易ログ
    best_iter = model.best_iteration
    best_score = model.best_score.get("valid", {}).get("auc")

    print("========================================")
    print(" TONOSAMA BUY MODEL TRAINED")
    print("----------------------------------------")
    print(f" best_iteration : {best_iter}")
    print(f" valid AUC      : {best_score}")
    print(f" model_path     : {MODEL_PATH}")
    print("========================================")


if __name__ == "__main__":
    main()