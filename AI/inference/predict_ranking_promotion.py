# ============================================================
# AI/inference/predict_ranking_promotion.py
# ------------------------------------------------------------
# ✔ ranking 昇格 → ENTRY 成功確率 推論
# ✔ LightGBM モデル使用
# ✔ 特徴量は学習時と完全一致
# ✔ ranking_trigger から直接呼べる
# ============================================================

import json
import logging
from pathlib import Path

import pandas as pd
import lightgbm as lgb

from config.paths import get_path

logger = logging.getLogger(__name__)

# ============================================================
# モデル / 特徴量
# ============================================================
MODEL_NAME = "ranking_promotion_lgbm"

MODEL_DIR = get_path("ai_models") / "ranking"
MODEL_PATH = MODEL_DIR / f"{MODEL_NAME}.txt"
FEATURE_PATH = MODEL_DIR / f"{MODEL_NAME}_features.json"

_model = None
_features = None


# ============================================================
def _load():
    global _model, _features

    if _model is not None:
        return

    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"ranking promotion model not found: {MODEL_PATH}")

    _model = lgb.Booster(model_file=str(MODEL_PATH))

    with open(FEATURE_PATH, "r", encoding="utf-8") as f:
        _features = json.load(f)

    logger.info(
        "🤖 ranking promotion model loaded (%d features)",
        len(_features),
    )


# ============================================================
def predict_ranking_promotion_proba(row: dict) -> float:
    """
    ranking_trigger 用 推論API

    row: ranking_trigger が生成した dict
    return: ENTRY 成功確率（0.0〜1.0）
    """

    _load()

    # DataFrame 化（1行）
    df = pd.DataFrame([row])

    # 欠損安全化
    for c in _features:
        if c not in df.columns:
            df[c] = 0

    X = df[_features]

    # categorical（学習時と一致）
    for c in X.columns:
        if X[c].dtype == "object":
            X[c] = X[c].astype("category")

    try:
        prob = float(_model.predict(X)[0])
        return prob
    except Exception as e:
        logger.error("❌ ranking promotion predict failed: %s", e, exc_info=True)
        return 0.0
