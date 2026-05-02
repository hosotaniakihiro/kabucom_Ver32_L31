# ============================================================
# File   : AI/train/train_exit_holdtime_lgbm.py
# Ver1.0-FINAL-EXIT-HOLDTIME
# ------------------------------------------------------------
# ✔ ai_entry_events.db をそのまま使用
# ✔ ENTRY時特徴量のみで最適ホールド時間を学習
# ✔ 最大MFEが出た秒数を回帰
# ✔ EXIT効率最適化用
# ============================================================

import sqlite3
import pickle
import logging
from pathlib import Path

import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error

logger = logging.getLogger(__name__)


# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_FILE = PROJECT_ROOT / "AI" / "data" / "ai_entry_events.db"
MODEL_DIR = PROJECT_ROOT / "AI" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODEL_DIR / "exit_holdtime_lgbm.pkl"


# ============================================================
# ENTRY FEATURE COLUMNS
# ============================================================

FEATURE_COLS = [
    "score_total",
    "dominant_ratio",
    "rsi",
    "macd",
    "ma_alignment",
    "volume",
    "index_score",
]


# ============================================================
# LABEL
# ============================================================

def make_label(df: pd.DataFrame) -> pd.Series:
    """
    ラベル：
    最大MFEが出た時点の holding_seconds
    """

    if "max_mfe_seconds" in df.columns:
        return df["max_mfe_seconds"]

    # fallback（なければ holding_seconds を使用）
    return df["holding_seconds"]


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
            AND holding_seconds IS NOT NULL
        """,
        con,
    )

    con.close()
    return df


# ============================================================
# TRAIN
# ============================================================

def train():
    logger.info("[EXIT HOLDTIME] loading data")
    df = load_training_data()

    df = df.dropna(subset=FEATURE_COLS + ["holding_seconds"])

    if len(df) < 500:
        raise RuntimeError("not enough training samples")

    X = df[FEATURE_COLS]
    y = make_label(df)

    X_train, X_valid, y_train, y_valid = train_test_split(
        X,
        y,
        test_size=0.2,
        random_state=42,
    )

    dtrain = lgb.Dataset(X_train, label=y_train)
    dvalid = lgb.Dataset(X_valid, label=y_valid)

    params = {
        "objective": "regression",
        "metric": "mae",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 30,
        "feature_fraction": 0.9,
        "verbosity": -1,
    }

    logger.info("[EXIT HOLDTIME] training start")

    model = lgb.train(
        params,
        dtrain,
        valid_sets=[dtrain, dvalid],
        valid_names=["train", "valid"],
        num_boost_round=500,
        early_stopping_rounds=50,
        verbose_eval=50,
    )

    y_pred = model.predict(X_valid)
    mae = mean_absolute_error(y_valid, y_pred)
    logger.info(f"[EXIT HOLDTIME] VALID MAE = {mae:.1f} sec")

    with open(MODEL_PATH, "wb") as f:
        pickle.dump(
            {
                "model": model,
                "features": FEATURE_COLS,
                "mae": mae,
            },
            f,
        )

    logger.info(f"[EXIT HOLDTIME] model saved → {MODEL_PATH}")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    train()
