# ============================================================
# File   : trading/ai/model_saver.py
# Version: FINAL-ROBUST-MODEL-SAVER
# ------------------------------------------------------------
# ✔ joblib保存
# ✔ ディレクトリ自動生成
# ✔ 例外耐性
# ============================================================

from __future__ import annotations
import os
import joblib
import logging

logger = logging.getLogger(__name__)


def save_model(model, path: str):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        joblib.dump(model, path)
        logger.info("[MODEL_SAVER] saved: %s", path)
    except Exception:
        logger.exception("[MODEL_SAVER] save failed")


def load_model(path: str):
    try:
        if not os.path.exists(path):
            logger.warning("[MODEL_SAVER] not found: %s", path)
            return None

        model = joblib.load(path)
        logger.info("[MODEL_SAVER] loaded: %s", path)
        return model

    except Exception:
        logger.exception("[MODEL_SAVER] load failed")
        return None