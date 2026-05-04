# ============================================================
# pj/trading/entry/ignition/holdtime_ai.py
# TONOSAMA 最適 holding 秒数 AI
# 作成日: 2025-12-31
# ------------------------------------------------------------
# ・ENTRY 前に最適保持秒数を推定
# ・発注は絶対にしない
# ・モデル未存在時は安全 fallback
# ============================================================

import joblib
from pathlib import Path
import numpy as np

# ============================================================
# 設定
# ============================================================
MODEL_PATH = Path("AI/model/tonosama_holdtime_lgbm.pkl")

FALLBACK_HOLD_SEC = 60     # モデル無し時
MIN_HOLD_SEC = 15          # 最低保持
MAX_HOLD_SEC = 120         # 最大保持


# ============================================================
# モデルキャッシュ
# ============================================================
_model = None


def _load_model():
    """
    LightGBM holding time モデルを遅延ロード
    """
    global _model

    if _model is not None:
        return _model

    if not MODEL_PATH.exists():
        return None

    try:
        _model = joblib.load(MODEL_PATH)
        return _model
    except Exception:
        _model = None
        return None


# ============================================================
# 推論
# ============================================================
def predict_hold_seconds(features: dict) -> int:
    """
    TONOSAMA holding 秒数を推論する

    features:
        {
            volume_speed: float
            fast_ret: float          # [%]
            rank_position: int
            price: float
            spread: float
            entry_second: int
        }

    return:
        int: 推奨 holding 秒数（安全ガード付き）
    """

    model = _load_model()
    if model is None:
        return FALLBACK_HOLD_SEC

    # --------------------------------------------------------
    # 特徴量（学習時と順序完全一致が前提）
    # --------------------------------------------------------
    try:
        X = np.array([[
            float(features.get("volume_speed", 0.0)),
            float(features.get("fast_ret", 0.0)),
            int(features.get("rank_position", 999)),
            float(features.get("price", 0.0)),
            float(features.get("spread", 0.0)),
            int(features.get("entry_second", 0)),
        ]], dtype=float)
    except Exception:
        return FALLBACK_HOLD_SEC

    # --------------------------------------------------------
    # 推論
    # --------------------------------------------------------
    try:
        sec = model.predict(X)[0]
    except Exception:
        return FALLBACK_HOLD_SEC

    # --------------------------------------------------------
    # 安全ガード
    # --------------------------------------------------------
    try:
        sec = int(sec)
    except Exception:
        return FALLBACK_HOLD_SEC

    return max(MIN_HOLD_SEC, min(sec, MAX_HOLD_SEC))
