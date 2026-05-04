# ============================================================
# AI/train/train_ranking_promotion_lgbm.py
# ------------------------------------------------------------
# ✔ ranking 昇格 → ENTRY 成否 / 利益判定 AI 学習
# ✔ LightGBM classification
# ✔ 時系列リーク防止（walk-forward 可能）
# ✔ モデル / 特徴量 / 学習ログ 完全保存
# ============================================================

import logging
import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import lightgbm as lgb
from sklearn.metrics import (
    accuracy_score,
    roc_auc_score,
    classification_report,
)

from AI.train.build_ranking_promotion_train_df import (
    build_ranking_promotion_train_df,
)
from config.paths import get_path

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================
MODEL_NAME = "ranking_promotion_lgbm"
TARGET_COL = "label_profit"     # 利益が出たか（pnl > 0）
TIME_COL = "triggered_at"

# 保存先
MODEL_DIR = get_path("ai_models") / "ranking"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODEL_DIR / f"{MODEL_NAME}.txt"
FEATURE_PATH = MODEL_DIR / f"{MODEL_NAME}_features.json"
LOG_PATH = MODEL_DIR / f"{MODEL_NAME}_train_log.txt"


# ============================================================
# 特徴量定義
# ============================================================
EXCLUDE_COLS = {
    "symbol",
    "reason",
    "created_at",
    "triggered_at",
    "pnl",
    "result",
    "label_entry",
    TARGET_COL,
}

CATEGORICAL_COLS = [
    "market",
    "rank_type",
]


# ============================================================
def split_train_valid(df: pd.DataFrame, ratio: float = 0.8):
    """
    時系列順 split（未来リーク防止）
    """
    split_idx = int(len(df) * ratio)
    return df.iloc[:split_idx], df.iloc[split_idx:]


# ============================================================
def train():
    logger.info("🚀 ranking promotion LGBM training start")

    # --------------------------------------------------------
    # データ作成
    # --------------------------------------------------------
    df = build_ranking_promotion_train_df(require_exit=True)

    if df.empty or len(df) < 100:
        logger.error("❌ training data insufficient")
        return

    # --------------------------------------------------------
    # 時系列ソート（安全）
    # --------------------------------------------------------
    if TIME_COL in df.columns:
        df = df.sort_values(TIME_COL).reset_index(drop=True)

    # --------------------------------------------------------
    # 特徴量選定
    # --------------------------------------------------------
    feature_cols = [
        c for c in df.columns
        if c not in EXCLUDE_COLS
    ]

    X = df[feature_cols]
    y = df[TARGET_COL]

    # --------------------------------------------------------
    # categorical 設定
    # --------------------------------------------------------
    for c in CATEGORICAL_COLS:
        if c in X.columns:
            X[c] = X[c].astype("category")

    # --------------------------------------------------------
    # train / valid split
    # --------------------------------------------------------
    X_train, X_valid = split_train_valid(X)
    y_train, y_valid = split_train_valid(y)

    logger.info(
        "📊 dataset rows=%d train=%d valid=%d",
        len(df),
        len(X_train),
        len(X_valid),
    )

    # --------------------------------------------------------
    # LightGBM Dataset
    # --------------------------------------------------------
    lgb_train = lgb.Dataset(
        X_train,
        y_train,
        categorical_feature=[
            c for c in CATEGORICAL_COLS if c in X.columns
        ],
        free_raw_data=False,
    )

    lgb_valid = lgb.Dataset(
        X_valid,
        y_valid,
        reference=lgb_train,
        free_raw_data=False,
    )

    # --------------------------------------------------------
    # パラメータ（ranking 特化）
    # --------------------------------------------------------
    params = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "boosting_type": "gbdt",
        "learning_rate": 0.03,
        "num_leaves": 31,
        "max_depth": -1,
        "min_data_in_leaf": 30,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "lambda_l1": 0.1,
        "lambda_l2": 0.1,
        "verbose": -1,
        "seed": 42,
    }

    # --------------------------------------------------------
    # 学習
    # --------------------------------------------------------
    model = lgb.train(
        params,
        lgb_train,
        num_boost_round=2000,
        valid_sets=[lgb_train, lgb_valid],
        valid_names=["train", "valid"],
        early_stopping_rounds=100,
        verbose_eval=100,
    )

    # --------------------------------------------------------
    # 評価
    # --------------------------------------------------------
    y_pred_prob = model.predict(X_valid, num_iteration=model.best_iteration)
    y_pred = (y_pred_prob >= 0.5).astype(int)

    acc = accuracy_score(y_valid, y_pred)
    auc = roc_auc_score(y_valid, y_pred_prob)

    logger.info("🎯 VALID ACC = %.4f", acc)
    logger.info("🎯 VALID AUC = %.4f", auc)

    logger.info("\n" + classification_report(y_valid, y_pred))

    # --------------------------------------------------------
    # 保存
    # --------------------------------------------------------
    model.save_model(str(MODEL_PATH))

    with open(FEATURE_PATH, "w", encoding="utf-8") as f:
        json.dump(feature_cols, f, ensure_ascii=False, indent=2)

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(
            f"\n[{datetime.now().isoformat()}]\n"
            f"rows={len(df)} train={len(X_train)} valid={len(X_valid)}\n"
            f"acc={acc:.4f} auc={auc:.4f}\n"
        )

    logger.info("💾 model saved: %s", MODEL_PATH)
    logger.info("💾 features saved: %s", FEATURE_PATH)
    logger.info("🎉 training completed")


# ============================================================
# entry point
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    train()
