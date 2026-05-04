# ============================================================
# AI/inference/exit_predictor.py
# Ver1.2.0-FINAL-EXIT-AI-PREDICTOR-STABLE-FAILSAFE-SHAP
# ------------------------------------------------------------
# ✔ EXIT AI 推論専用レイヤ
# ✔ LightGBM モデル読み込み（スレッドセーフ）
# ✔ モデル未存在でも絶対に落ちない（フェイルセーフ）
# ✔ meta未存在でも絶対に落ちない
# ✔ SHAP 付き推論（観測専用）対応
# ✔ SHAP ログは副作用ゼロ
# ✔ state_machine / exit_controller 安全連携
# ✔ 本番安定運用仕様
# ============================================================

from __future__ import annotations

import json
import threading
import logging
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import lightgbm as lgb

from config.paths import get_path

logger = logging.getLogger(__name__)

# ============================================================
# グローバル（スレッドセーフ）
# ============================================================

_MODEL_LOCK = threading.Lock()
_MODEL: Optional[lgb.Booster] = None
_META: Optional[dict] = None
_MODEL_ATTEMPTED = False  # 二度ロード試行しない

# ============================================================
# パス
# ============================================================

MODEL_DIR: Path = get_path("ai_model_exit")
MODEL_FILE: Path = MODEL_DIR / "exit_ai_lgbm.txt"
META_FILE: Path = MODEL_DIR / "exit_ai_meta.json"

# ============================================================
# モデルロード（完全フェイルセーフ）
# ============================================================

def _load_model() -> None:
    """
    EXIT AI モデル & meta を 1 回だけロード
    フェイルセーフ設計
    """
    global _MODEL, _META, _MODEL_ATTEMPTED

    with _MODEL_LOCK:
        if _MODEL_ATTEMPTED:
            return

        _MODEL_ATTEMPTED = True

        try:
            if not MODEL_FILE.exists():
                logger.warning(f"[EXIT_AI] model not found → AI disabled: {MODEL_FILE}")
                _MODEL = None
                return

            _MODEL = lgb.Booster(model_file=str(MODEL_FILE))
            logger.info("[EXIT_AI] model loaded")

            if META_FILE.exists():
                with open(META_FILE, "r", encoding="utf-8") as f:
                    _META = json.load(f)
            else:
                logger.warning("[EXIT_AI] meta file not found → AI disabled")
                _MODEL = None
                return

            if not _META.get("features"):
                logger.warning("[EXIT_AI] meta missing features → AI disabled")
                _MODEL = None
                return

        except Exception as e:
            logger.error(f"[EXIT_AI] model load failed → AI disabled: {e}")
            _MODEL = None

# ============================================================
# 特徴量整形
# ============================================================

def _build_feature_vector(features: Dict) -> Optional[np.ndarray]:
    """
    推論用特徴量ベクトル生成
    """
    _load_model()

    if _MODEL is None or _META is None:
        return None

    try:
        feature_names = _META["features"]

        vec = []
        for name in feature_names:
            v = features.get(name)
            try:
                vec.append(float(v))
            except Exception:
                vec.append(0.0)

        return np.array(vec, dtype=np.float32).reshape(1, -1)

    except Exception as e:
        logger.error(f"[EXIT_AI] feature build failed: {e}")
        return None

# ============================================================
# 基本推論（高速・実運用）
# ============================================================

def predict_exit_mode(features: Dict) -> Dict:
    """
    EXIT AI 推論（SHAPなし・高速）

    return:
      {
        "exit_mode": -1 | 0 | 1,
        "proba": { "-1": p, "0": p, "1": p },
        "confidence": float,
        "ai_enabled": bool
      }
    """

    try:
        X = _build_feature_vector(features)

        if X is None or _MODEL is None:
            return {
                "exit_mode": 0,
                "proba": {"-1": 0.0, "0": 1.0, "1": 0.0},
                "confidence": 0.0,
                "ai_enabled": False,
            }

        probs = _MODEL.predict(X)[0]

        idx = int(np.argmax(probs))
        exit_mode = idx - 1
        confidence = float(np.max(probs))

        return {
            "exit_mode": exit_mode,
            "proba": {
                "-1": float(probs[0]),
                "0": float(probs[1]),
                "1": float(probs[2]),
            },
            "confidence": confidence,
            "ai_enabled": True,
        }

    except Exception as e:
        logger.error(f"[EXIT_AI] inference failed → AI disabled: {e}")
        return {
            "exit_mode": 0,
            "proba": {"-1": 0.0, "0": 1.0, "1": 0.0},
            "confidence": 0.0,
            "ai_enabled": False,
        }

# ============================================================
# SHAP 付き推論（観測専用）
# ============================================================

def predict_exit_mode_with_shap(
    features: Dict,
    *,
    log: bool = True,
) -> Dict:
    """
    EXIT AI 推論 + SHAP（重い・観測専用）
    """

    result = predict_exit_mode(features)

    if not result.get("ai_enabled"):
        return result

    if not log:
        return result

    try:
        import shap

        X = _build_feature_vector(features)
        if X is None:
            return result

        explainer = shap.TreeExplainer(_MODEL)
        shap_values = explainer.shap_values(X)

        shap_result = {
            "exit_mode": result["exit_mode"],
            "confidence": result["confidence"],
            "features": _META["features"],
            "shap_values": [
                float(v)
                for v in shap_values[result["exit_mode"] + 1][0]
            ],
        }

        from AI.logs.exit_shap_logger import save_exit_shap_log

        save_exit_shap_log(
            shap_result=shap_result,
            raw_features=features,
        )

    except Exception as e:
        result["shap_error"] = str(e)

    return result

# ============================================================
# state_machine / exit_controller 用ラッパ
# ============================================================

def should_block_exit_by_ai(
    features: Dict,
    *,
    min_confidence: float = 0.65,
) -> bool:
    """
    AI による EXIT 抑制判定

    True  : EXIT を止める
    False : EXIT を許可
    """

    res = predict_exit_mode(features)

    # AI無効なら絶対止めない
    if not res.get("ai_enabled"):
        return False

    # 「良い EXIT (1) ではない」かつ「自信が高い」場合だけ抑制
    if res["exit_mode"] != 1 and res["confidence"] >= min_confidence:
        return True

    return False
