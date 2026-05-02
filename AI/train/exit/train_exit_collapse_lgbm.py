# ============================================================
# File   : AI/train/exit/train_exit_collapse_lgbm.py
# Version: V31-FINAL-COLLAPSE-TRAIN-LONG-SHORT
# ------------------------------------------------------------
# ✔ Long / Short 別モデル対応
# ✔ class_weight balanced
# ✔ NaN完全吸収
# ✔ 特徴量検証
# ✔ config.paths 統合
# ✔ 例外安全
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path

from config.paths import get_path

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

FEATURE_COLUMNS = [
    "ma75_slope",
    "ranking_delta",
    "ranking_persistence",
    "mfe_from_peak",
    "volume_decay",
    "spread_expansion",
    "atr_ratio",
    "regime",
]

TARGET_COLUMN = "collapse_target"


# ============================================================
# Utility
# ============================================================

def _validate_columns(df: pd.DataFrame):

    missing = [c for c in FEATURE_COLUMNS + [TARGET_COLUMN] if c not in df.columns]

    if missing:
        raise ValueError(f"Missing required columns: {missing}")


def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    # NaN吸収
    df[FEATURE_COLUMNS] = df[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan)
    df[FEATURE_COLUMNS] = df[FEATURE_COLUMNS].fillna(0.0)

    df[TARGET_COLUMN] = df[TARGET_COLUMN].fillna(0).astype(int)

    return df


# ============================================================
# Core Train Function
# ============================================================

def train_collapse(
    df: pd.DataFrame,
    side: str = "LONG",
    save: bool = True
):

    """
    side:
        "LONG" or "SHORT"
    """

    try:
        logger.info(f"[TRAIN] collapse model start ({side})")

        _validate_columns(df)
        df = _sanitize_dataframe(df)

        X = df[FEATURE_COLUMNS]
        y = df[TARGET_COLUMN]

        model = lgb.LGBMClassifier(
            n_estimators=400,
            learning_rate=0.04,
            max_depth=6,
            subsample=0.8,
            colsample_bytree=0.8,
            class_weight="balanced",
            random_state=42
        )

        model.fit(X, y)

        logger.info(
            f"[TRAIN] collapse model trained ({side}) | "
            f"samples={len(df)} | positives={int(y.sum())}"
        )

        if save:
            _save_model(model, side)

        return model

    except Exception:
        logger.exception("[TRAIN] collapse training failed")
        raise


# ============================================================
# Save
# ============================================================

def _save_model(model, side: str):

    model_dir: Path = get_path("ai_model_exit")
    model_dir.mkdir(parents=True, exist_ok=True)

    filename = f"collapse_{side.lower()}.pkl"
    save_path = model_dir / filename

    joblib.dump(model, save_path)

    logger.info(f"[TRAIN] collapse model saved → {save_path}")


# ============================================================
# Entry Point (optional batch use)
# ============================================================

if __name__ == "__main__":

    logger.info("[TRAIN] collapse training script started")

    # 例: CSV読み込み
    train_dir = get_path("ai_train_exit")
    csv_path = train_dir / "collapse_train.csv"

    if not csv_path.exists():
        raise FileNotFoundError(f"Training file not found: {csv_path}")

    df = pd.read_csv(csv_path)

    # LONG
    train_collapse(df[df["side"] == "LONG"], side="LONG")

    # SHORT
    train_collapse(df[df["side"] == "SHORT"], side="SHORT")

    logger.info("[TRAIN] collapse training completed")