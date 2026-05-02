# ============================================================
# File   : AI/train/exit/train_collapse_model.py
# Version: V32-FINAL-COLLAPSE-TRAINER
# ------------------------------------------------------------
# ✔ LONG / SHORT 別モデル学習
# ✔ class_weight="balanced"
# ✔ NaN / inf 完全吸収
# ✔ config.paths 統合
# ✔ UNC安全
# ✔ 例外安全
# ✔ 将来SHAP対応可能
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
import lightgbm as lgb
import joblib
from pathlib import Path

from config.paths import get_path, ensure_dirs

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


def _sanitize(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.fillna(0.0)

    df[TARGET_COLUMN] = df[TARGET_COLUMN].astype(int)

    return df


# ============================================================
# Train
# ============================================================

def train_collapse_model(
    input_csv: Path,
    side: str,
    save: bool = True
):

    try:
        logger.info("[TRAIN] Loading dataset: %s", input_csv)

        df = pd.read_csv(input_csv)

        if df.empty:
            logger.warning("[TRAIN] Empty dataset.")
            return None

        _validate_columns(df)
        df = _sanitize(df)

        X = df[FEATURE_COLUMNS]
        y = df[TARGET_COLUMN]

        logger.info(
            "[TRAIN] side=%s samples=%d positives=%d",
            side,
            len(df),
            int(y.sum())
        )

        model = lgb.LGBMClassifier(
            n_estimators=500,
            learning_rate=0.03,
            max_depth=6,
            subsample=0.8,
            colsample_bytree=0.8,
            class_weight="balanced",
            random_state=42
        )

        model.fit(X, y)

        logger.info("[TRAIN] Model trained successfully.")

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

    ensure_dirs()

    model_dir = get_path("ai_model_exit")
    model_dir.mkdir(parents=True, exist_ok=True)

    filename = f"collapse_{side.lower()}.pkl"
    save_path = model_dir / filename

    joblib.dump(model, save_path)

    logger.info("[TRAIN] Saved model → %s", save_path)


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    ensure_dirs()

    train_dir = get_path("ai_train_exit")

    long_csv = train_dir / "collapse_train_long.csv"
    short_csv = train_dir / "collapse_train_short.csv"

    if long_csv.exists():
        train_collapse_model(long_csv, side="LONG")

    if short_csv.exists():
        train_collapse_model(short_csv, side="SHORT")

    logger.info("[TRAIN] Collapse training completed.")