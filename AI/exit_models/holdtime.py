# ============================================================
# File   : AI/exit_models/holdtime.py
# Ver1.1-FINAL-EXIT-HOLDTIME-PREDICTOR-SAFE
# ------------------------------------------------------------
# ✔ EXITホールド時間AI 推論ラッパー
# ✔ ENTRY時特徴量 → 最適ホールド秒数を推定
# ✔ LightGBM 回帰モデル対応
# ✔ 遅延ロード / キャッシュ
# ✔ 失敗時は None を返す（上位で判断しない）
# ✔ モデル未存在でもシステム停止しない
# ✔ 破損モデル完全防御
# ✔ 異常値完全ガード
# ============================================================

from __future__ import annotations

import pickle
import logging
import math
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = PROJECT_ROOT / "AI" / "models" / "exit_holdtime_lgbm.pkl"

# ============================================================
# INTERNAL CACHE
# ============================================================

_model = None
_features = None
_model_loaded = False
_model_load_failed = False


# ============================================================
# LOAD MODEL（遅延ロード・安全版）
# ============================================================

def _load_model():
    global _model, _features, _model_loaded, _model_load_failed

    # 既にロード済
    if _model_loaded:
        return

    # 以前にロード失敗している場合は再試行しない
    if _model_load_failed:
        return

    if not MODEL_PATH.exists():
        logger.warning(
            "[EXIT HOLDTIME] model not found → fallback mode: %s",
            MODEL_PATH,
        )
        _model_load_failed = True
        return

    try:
        with open(MODEL_PATH, "rb") as f:
            obj = pickle.load(f)

        # 必須キー検証
        if not isinstance(obj, dict):
            raise ValueError("Invalid model file structure")

        if "model" not in obj or "features" not in obj:
            raise ValueError("Model file missing required keys")

        _model = obj["model"]
        _features = obj["features"]

        if not isinstance(_features, list):
            raise ValueError("Invalid feature list")

        _model_loaded = True

        logger.info("[EXIT HOLDTIME] model loaded successfully")

    except Exception:
        logger.exception("[EXIT HOLDTIME] model load failed → fallback")
        _model_load_failed = True
        _model = None
        _features = None


# ============================================================
# PUBLIC API
# ============================================================

def predict_exit_holdtime(entry_features: Dict[str, Any]) -> Optional[float]:
    """
    EXIT ホールド時間AI 推論

    Args:
        entry_features (dict):
            ENTRY時点の特徴量

    Returns:
        float | None:
            最適ホールド秒数（>0）
            失敗時は None
    """

    try:
        if not isinstance(entry_features, dict):
            return None

        _load_model()

        # モデル無し fallback
        if _model is None or not _features:
            return None

        # 学習時順序で特徴量整形
        x = [
            float(entry_features.get(col, 0.0))
            for col in _features
        ]

        # 2次元化（sklearn / lgbm 用）
        pred = _model.predict([x])

        if not pred:
            return None

        seconds = float(pred[0])

        # ----------------------------------------------------
        # 異常値ガード
        # ----------------------------------------------------
        if (
            seconds is None
            or math.isnan(seconds)
            or math.isinf(seconds)
            or seconds <= 0
            or seconds > 60 * 60  # 1時間超は拒否
        ):
            return None

        return seconds

    except Exception:
        logger.exception("[EXIT HOLDTIME] prediction failed")
        return None