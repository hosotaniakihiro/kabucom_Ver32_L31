# ============================================================
# AI/train/entry/tonosama_entry_lgbm.py
# TONOSAMA ENTRY 最終可否AI
# ------------------------------------------------------------
# ✔ LightGBM Binary Classifier
# ✔ ai_boost 後段専用（方式選択後）
# ✔ BUY / SELL 共通
# ✔ 失敗時は必ず False（安全側）
# ============================================================

from pathlib import Path
import joblib
import logging

logger = logging.getLogger(__name__)

# ============================================================
# PATH
# ============================================================

MODEL_PATH = Path("AI/model/tonosama_entry_lgbm.pkl")

# ============================================================
# FEATURES（train と完全一致）
# ============================================================

FEATURES = [
    "fast_ret",        # 初動リターン（5秒）
    "volume_speed",    # 出来高速度
    "entry_second",    # エントリー秒（0-59）
    "entry_mode_id",   # BREAKOUT=1 / PULLBACK=0
]

# ============================================================
# 内部モデル
# ============================================================

_model = None


# ============================================================
# モデルロード（遅延）
# ============================================================

def _load_model():
    global _model
    if _model is not None:
        return _model

    if not MODEL_PATH.exists():
        logger.warning(f"[TONOSAMA_ENTRY_AI] model not found: {MODEL_PATH}")
        return None

    try:
        _model = joblib.load(MODEL_PATH)
        logger.info("[TONOSAMA_ENTRY_AI] model loaded")
        return _model
    except Exception:
        logger.exception("[TONOSAMA_ENTRY_AI] model load failed")
        _model = None
        return None


# ============================================================
# 推論
# ============================================================

def predict_tonosama_entry(features: dict) -> bool:
    """
    TONOSAMA ENTRY 最終可否判定

    Parameters
    ----------
    features : dict
        {
            fast_ret: float,
            volume_speed: float,
            entry_second: int,
            entry_mode: str ("BREAKOUT" / "PULLBACK")
        }

    Returns
    -------
    bool
        True  -> ENTRY OK
        False -> ENTRY SKIP
    """

    model = _load_model()

    # --------------------------------------------------------
    # フォールバック（モデル未使用時は安全側）
    # --------------------------------------------------------
    if model is None:
        return False

    try:
        entry_mode = features.get("entry_mode", "BREAKOUT")
        entry_mode_id = 1 if entry_mode == "BREAKOUT" else 0

        X = [[
            float(features.get("fast_ret", 0.0)),
            float(features.get("volume_speed", 0.0)),
            int(features.get("entry_second", 0)),
            entry_mode_id,
        ]]

        pred = model.predict(X)[0]

        # Binary: 1 = ENTRY OK / 0 = SKIP
        return bool(pred == 1)

    except Exception:
        logger.exception("[TONOSAMA_ENTRY_AI] inference failed")
        return False
