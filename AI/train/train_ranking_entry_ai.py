# ============================================================
# AI/train/train_ranking_entry_ai.py
# ------------------------------------------------------------
# ランキング由来 ENTRY 可否判定 AI（LightGBM）
#
# ✔ ranking snapshot / pending_entries 学習対応
# ✔ BUY / SELL / NONE（3-class classification）
# ✔ future leak 完全防止（確定後リターンのみ使用）
# ✔ cluster 非依存（汎用モデル）
# ✔ entry_gate から predict() で直接利用可能
# ✔ Ver7.x ranking entry pipeline 対応
# ============================================================

import logging
import json
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from sklearn.preprocessing import LabelEncoder
import joblib

# ------------------------------------------------------------
# logging
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# paths
# ------------------------------------------------------------
BASE_DIR = Path("AI")
DATA_DIR = BASE_DIR / "data"
MODEL_DIR = BASE_DIR / "model"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

TRAIN_CSV = DATA_DIR / "ranking_entry_train.csv"
MODEL_FILE = MODEL_DIR / "ranking_entry_lgbm.pkl"
FEATURE_FILE = MODEL_DIR / "ranking_entry_features.json"

# ------------------------------------------------------------
# label 定義
# ------------------------------------------------------------
LABEL_BUY = "BUY"
LABEL_SELL = "SELL"
LABEL_NONE = "NONE"

# ------------------------------------------------------------
# 使用特徴量（ranking + price action + micro structure）
# ------------------------------------------------------------
FEATURE_COLUMNS: List[str] = [
    # ranking
    "rank",
    "rank_diff",
    "volume_speed",
    "ranking_score",

    # price
    "close",
    "open",
    "high",
    "low",
    "spread",

    # momentum
    "price_change_pct",
    "price_velocity",
    "vwap_distance",

    # micro
    "atr",
    "atr_ratio",
    "volatility_1m",

    # market context
    "index_return_1m",
    "index_volatility",

    # flags
    "is_breakout",
    "is_pullback",
]

TARGET_COLUMN = "entry_label"

# ------------------------------------------------------------
def load_training_data() -> pd.DataFrame:
    """
    学習用 CSV をロード
    ranking snapshot / pending_entries から事前生成されている前提
    """
    if not TRAIN_CSV.exists():
        raise FileNotFoundError(f"❌ training data not found: {TRAIN_CSV}")

    df = pd.read_csv(TRAIN_CSV)
    logger.info(f"📊 training data loaded rows={len(df)}")
    return df


# ------------------------------------------------------------
def preprocess(df: pd.DataFrame):
    """
    前処理
    """
    # 必須列チェック
    missing = set(FEATURE_COLUMNS + [TARGET_COLUMN]) - set(df.columns)
    if missing:
        raise ValueError(f"❌ missing columns: {missing}")

    # 無限大・NaN 対策
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=FEATURE_COLUMNS + [TARGET_COLUMN])

    # label encode
    le = LabelEncoder()
    y = le.fit_transform(df[TARGET_COLUMN])

    X = df[FEATURE_COLUMNS]

    return X, y, le


# ------------------------------------------------------------
def train():
    logger.info("🚀 train_ranking_entry_ai START")

    df = load_training_data()
    X, y, label_encoder = preprocess(df)

    # train / valid split（時系列リーク防止のため shuffle=False 推奨）
    X_train, X_valid, y_train, y_valid = train_test_split(
        X,
        y,
        test_size=0.2,
        shuffle=False,
    )

    logger.info(
        f"split train={len(X_train)} valid={len(X_valid)}"
    )

    # LightGBM params
    params = {
        "objective": "multiclass",
        "num_class": len(label_encoder.classes_),
        "metric": "multi_logloss",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 50,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 5,
        "verbosity": -1,
        "seed": 42,
    }

    lgb_train = lgb.Dataset(X_train, label=y_train)
    lgb_valid = lgb.Dataset(X_valid, label=y_valid)

    model = lgb.train(
        params,
        lgb_train,
        valid_sets=[lgb_train, lgb_valid],
        valid_names=["train", "valid"],
        num_boost_round=1000,
        early_stopping_rounds=50,
        verbose_eval=50,
    )

    # --------------------------------------------------------
    # evaluation
    # --------------------------------------------------------
    y_pred = np.argmax(model.predict(X_valid), axis=1)

    report = classification_report(
        y_valid,
        y_pred,
        target_names=label_encoder.classes_,
    )
    logger.info("\n" + report)

    # --------------------------------------------------------
    # save model
    # --------------------------------------------------------
    joblib.dump(
        {
            "model": model,
            "label_encoder": label_encoder,
        },
        MODEL_FILE,
    )

    with open(FEATURE_FILE, "w", encoding="utf-8") as f:
        json.dump(FEATURE_COLUMNS, f, indent=2, ensure_ascii=False)

    logger.info(f"💾 model saved -> {MODEL_FILE}")
    logger.info(f"💾 feature list saved -> {FEATURE_FILE}")
    logger.info("✅ train_ranking_entry_ai DONE")


# ------------------------------------------------------------
if __name__ == "__main__":
    train()
