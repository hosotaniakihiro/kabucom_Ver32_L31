# ============================================================
# File   : AI/train/train_exit_collapse_lgbm.py
# Ver1.0-FINAL-EXIT-COLLAPSE
# ------------------------------------------------------------
# ✔ 固定ストップ完全廃止
# ✔ 逆行後に「戻らない」パターンを学習
# ✔ Smart Stop（期待値ゼロ検知）
# ✔ ai_entry_events.db をそのまま使用
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

MODEL_PATH = MODEL_DIR / "exit_collapse_lgbm.pkl"


# ============================================================
# FEATURE COLUMNS（EXIT時点）
# ============================================================

FEATURE_COLS = [
    # price behavior
    "price_from_entry",
    "price_velocity",
    "max_mae",

    # volume / liquidity
    "volume_decay",

    # trend / structure
    "ma5_slope",
    "vwap_diff",
    "score_decay",

    # environment
    "index_shock",
    "elapsed_seconds",
]


# ============================================================
# LABEL
# ============================================================

def make_label(df: pd.DataFrame) -> pd.Series:
    """
    ラベル定義：
    1 = この逆行後、一度もプラスに戻らなかった（崩壊）
    0 = 一時逆行したが、その後プラス回復した
    """

    # 条件：
    # max_mfe <= 0 → 一度も利益になっていない
    # OR
    # max_mfe < abs(max_mae) → 逆行が支配的

    label = (
        (df["max_mfe"] <= 0)
        | (df["max_mfe"] < df["max_mae"].abs())
    ).astype(int)

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
            AND max_mae IS NOT NULL
            AND max_mfe IS NOT NULL
        """,
        con,
    )

    con.close()
    return df


# ============================================================
# TRAIN
# ============================================================

def train():
    logger.info("[EXIT COLLAPSE] loading data")
    df = load_training_data()

    df = df.dropna(subset=FEATURE_COLS + ["max_mfe", "max_mae"])

    if len(df) < 500:
        raise RuntimeError("not enough training samples")

    X = df[FEATURE_COLS]
    y = make_label(df)

    X_train, X_valid, y_train, y_valid = train_test_split(
        X,
        y,
        test_size=0.2,
        shuffle=True,
        random_state=42,
    )

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

    logger.info("[EXIT COLLAPSE] training start")

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
    auc = roc_auc_score(y_valid, y_pred)
    logger.info(f"[EXIT COLLAPSE] VALID AUC = {auc:.4f}")

    with open(MODEL_PATH, "wb") as f:
        pickle.dump(
            {
                "model": model,
                "features": FEATURE_COLS,
                "auc": auc,
            },
            f,
        )

    logger.info(f"[EXIT COLLAPSE] model saved → {MODEL_PATH}")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    train()
