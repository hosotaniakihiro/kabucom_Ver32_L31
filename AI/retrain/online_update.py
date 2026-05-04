# ============================================================
# online_update.py
# LightGBM 既存モデルの追加学習（Partial Update）
# ------------------------------------------------------------
# ⚠️ 本番売買ループからは絶対に呼ばない
# ⚠️ 引け後 / バッチ / 手動実行専用
# ============================================================

import joblib
import pandas as pd
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

MODEL_PATH = Path("AI/model/final_decision_lgbm.pkl")


def load_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"model not found: {MODEL_PATH}")
    return joblib.load(MODEL_PATH)


def save_model(model):
    joblib.dump(model, MODEL_PATH)
    logger.info(f"✅ model updated: {MODEL_PATH}")


def partial_update(model, X_new, y_new):
    """
    既存 LightGBM モデルに対して追加学習を行う

    Parameters
    ----------
    model : LGBMClassifier
        既存学習済みモデル
    X_new : pd.DataFrame
        追加学習用特徴量
    y_new : pd.Series
        追加学習用ラベル
    """

    if X_new.empty or y_new.empty:
        logger.warning("❌ partial_update skipped (empty data)")
        return model

    model.fit(
        X_new,
        y_new,
        init_model=model,
        keep_training_booster=True,
    )

    return model
