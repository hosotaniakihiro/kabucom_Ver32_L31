# ============================================================
# File   : AI/train/train_exit_takeprofit_lgbm.py
# Ver1.0-FINAL-EXIT-TAKEPROFIT
# ------------------------------------------------------------
# ✔ ai_entry_events.db をそのまま使用
# ✔ EXIT時点の特徴量で「今利確すべきか」を学習
# ✔ 勝ち逃げ成功 / まだ待つ の2値分類
# ✔ LightGBM + feature importance 保存
# ============================================================

import sqlite3
import pickle
import logging
from pathlib import Path

import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

logger = logging.getLogger(__name__)


# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_FILE = PROJECT_ROOT / "AI" / "data" / "ai_entry_events.db"
MODEL_DIR = PROJECT_ROOT / "AI" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODEL_DIR / "exit_takeprofit_lgbm.pkl"


# ============================================================
# FEATURE COLUMNS（EXIT時点）
# ============================================================

FEATURE_COLS = [
    # entry related
    "score_total",
    "dominant_ratio",
    "entry_price",

    # market / momentum
    "price_from_entry",
    "price_velocity",
    "volume_decay",
    "vwap_diff",
    "ma5_slope",
    "rsi",
    "macd",

    # environment
    "index_shock",
    "elapsed_seconds",
]


# ============================================================
# LABEL DEFINITION
# ============================================================

def make_label(df: pd.DataFrame) -> pd.Series:
    """
    ラベル定義：
    1 = EXIT時点で、最終MFEの80%以上を確保できていた
    0 = もっと待てば明確に伸びた
    """

    # 安全ガード
    if "max_mfe" not in df.columns or "price_from_entry" not in df.columns:
        raise ValueError("required columns missing for label")

    current_profit = df["price_from_entry"]
    max_profit = df["max_mfe"].replace(0, pd.NA)

    ratio = current_profit / max_profit
    label = (ratio >= 0.80).astype(int)

    return label


# ============================================================
# LOAD DATA
# ============================================================

def load_training_data() -> pd.DataFrame:
    if not DB_FILE.exists():
        raise FileNotFoundError(DB_FILE)

    con = sqlite3.connect(DB_FILE)

    df = pd.read_sql(
        """
        SELECT
            *
        FROM entry_events
        WHERE
            exit_time IS NOT NULL
            AND max_mfe IS NOT NULL
            AND elapsed_seconds IS NOT NULL
        """,
        con,
    )

    con.close()

    return df


# ============================================================
# TRAIN
# ============================================================

def train():
    logger.info("[EXIT TAKEPROFIT] loading data")
    df = load_training_data()

    # 欠損除去
    df = df.dropna(subset=FEATURE_COLS + ["max_mfe"])

    if len(df) < 500:
        raise RuntimeError("not enough training samples")

    # ラベル生成
    y = make_label(df)
    X = df[FEATURE_COLS]

    # train / valid split
    X_train, X_valid, y_train, y_valid = train_test_split(
        X,
        y,
        test_size=0.2,
        shuffle=True,
        random_state=42,
    )

    # LightGBM Dataset
    dtrain = lgb.Dataset(X_train, label=y_train)
    dvalid = lgb.Dataset(X_valid, label=y_valid)

    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 30,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 5,
        "verbosity": -1,
    }

    logger.info("[EXIT TAKEPROFIT] training start")

    model = lgb.train(
        params,
        dtrain,
        valid_sets=[dtrain, dvalid],
        valid_names=["train", "valid"],
        num_boost_round=500,
        early_stopping_rounds=50,
        verbose_eval=50,
    )

    # 評価
    y_pred = model.predict(X_valid)
    auc = roc_auc_score(y_valid, y_pred)
    logger.info(f"[EXIT TAKEPROFIT] VALID AUC = {auc:.4f}")

    # 保存
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(
            {
                "model": model,
                "features": FEATURE_COLS,
                "auc": auc,
            },
            f,
        )

    logger.info(f"[EXIT TAKEPROFIT] model saved → {MODEL_PATH}")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    train()
