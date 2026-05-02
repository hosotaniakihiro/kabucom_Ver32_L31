# ============================================================
# File   : AI/exit_models/takeprofit.py
# Version: Ver1.1-FINAL-EXIT-TAKEPROFIT-PREDICTOR-STABLE
# ------------------------------------------------------------
# ✔ EXIT即時利益AI 推論ラッパー
# ✔ LightGBM / sklearn 二値分類対応
# ✔ predict_proba / predict 両対応
# ✔ 遅延ロード / キャッシュ
# ✔ モデル未配置でも安全フォールバック
# ✔ ログスパム防止
# ✔ features dict → 利確確率(float)
# ✔ 副作用なし / 失敗時は 0.0
# ============================================================

from __future__ import annotations

import pickle
from pathlib import Path
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = PROJECT_ROOT / "AI" / "models" / "exit_takeprofit_lgbm.pkl"

# ============================================================
# INTERNAL CACHE
# ============================================================

_model: Any = None
_features: list[str] | None = None
_model_loaded: bool = False
_model_load_failed: bool = False


# ============================================================
# LOAD MODEL（遅延ロード / 安全版）
# ============================================================

def _load_model() -> None:
    """
    モデルを一度だけロードする
    失敗時は例外を投げず、安全フォールバックへ
    """
    global _model, _features, _model_loaded, _model_load_failed

    if _model_loaded or _model_load_failed:
        return

    if not MODEL_PATH.exists():
        logger.warning(
            "[EXIT TAKEPROFIT] model not found → fallback mode (%s)",
            MODEL_PATH,
        )
        _model_load_failed = True
        return

    try:
        with open(MODEL_PATH, "rb") as f:
            obj = pickle.load(f)

        if not isinstance(obj, dict):
            logger.error("[EXIT TAKEPROFIT] invalid model format")
            _model_load_failed = True
            return

        _model = obj.get("model")
        _features = obj.get("features")

        if _model is None or not isinstance(_features, list):
            logger.error("[EXIT TAKEPROFIT] model or features missing")
            _model_load_failed = True
            return

        _model_loaded = True
        logger.info("[EXIT TAKEPROFIT] model loaded successfully")

    except Exception:
        logger.exception("[EXIT TAKEPROFIT] model load failed")
        _model_load_failed = True


# ============================================================
# PUBLIC API
# ============================================================

def predict_exit_takeprofit(features: dict) -> float:
    """
    EXIT 即時利益AI 推論

    Args:
        features (dict):
            EXIT時点の特徴量

    Returns:
        float:
            今EXITすべき確率（0.0〜1.0）
    """

    try:
        if not isinstance(features, dict):
            return 0.0

        _load_model()

        if not _model_loaded or _model is None or not _features:
            return 0.0

        # ====================================================
        # 特徴量整形（欠損は0）
        # ====================================================
        x = [[float(features.get(col, 0.0) or 0.0) for col in _features]]

        # ====================================================
        # 推論
        # ====================================================
        if hasattr(_model, "predict_proba"):
            prob = float(_model.predict_proba(x)[0][1])
        else:
            # 回帰 or 二値predict想定
            prob = float(_model.predict(x)[0])

        # ====================================================
        # ガード
        # ====================================================
        if prob < 0.0:
            return 0.0
        if prob > 1.0:
            return 1.0

        return prob

    except Exception:
        logger.exception("[EXIT TAKEPROFIT] prediction failed")
        return 0.0