# ============================================================
# File   : trading/learning/online_trainer.py
# Version: FINAL-ROBUST-ONLINE-TRAINER
# ------------------------------------------------------------
# ✔ reward DB読み込み
# ✔ 特徴量指定学習
# ✔ 再学習対応
# ✔ 例外耐性
# ✔ 将来LightGBM対応可能
# ============================================================

from __future__ import annotations
import pandas as pd
import sqlite3
import logging

logger = logging.getLogger(__name__)


def fetch_training_data(db_path="reward.db") -> pd.DataFrame:
    try:
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql("SELECT * FROM reward_log", conn)
        return df
    except Exception:
        logger.exception("[ONLINE_TRAINER] fetch failed")
        return pd.DataFrame()


def retrain_model(
    df: pd.DataFrame,
    model,
    feature_cols: list[str],
    target_col: str = "reward"
):

    if df is None or df.empty:
        logger.warning("[ONLINE_TRAINER] no data")
        return model

    try:
        X = df[feature_cols].fillna(0)
        y = df[target_col].fillna(0)

        model.fit(X, y)

        logger.info("[ONLINE_TRAINER] retrain completed")
        return model

    except Exception:
        logger.exception("[ONLINE_TRAINER] retrain failed")
        return model