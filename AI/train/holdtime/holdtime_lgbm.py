# ============================================================
# AI/train/holdtime/holdtime_lgbm.py
# HOLDTIME 推論AI（あと何秒 HOLD すべきか）
# ------------------------------------------------------------
# ✔ LightGBM Regressor
# ✔ EXIT抑制専用（EXIT強制はしない）
# ✔ entry_controller / exit_controller 完全互換
# ✔ 学習FEATURE完全一致
# ✔ フォールバック安全設計（暴発防止）
# ============================================================

from pathlib import Path
import joblib
import logging
import math

logger = logging.getLogger(__name__)

# ============================================================
# PATH
# ============================================================

MODEL_PATH = Path("AI/model/holdtime_lgbm.pkl")

# ============================================================
# FEATURES（train と完全一致・順序固定）
# ============================================================

FEATURES = [
    "profit_rate",      # 現在損益率 [%]
    "drawdown_rate",    # 押し率（負値）
    "volume_speed",     # 出来高速度
    "volatility",       # ボラティリティ
    "trend_strength",   # トレンド強度
    "hold_seconds",     # 現在の保持秒
]

# ============================================================
# 内部モデル（遅延ロード）
# ============================================================

_model = None


def _load_model():
    """モデル遅延ロード（1回のみ）"""
    global _model

    if _model is not None:
        return _model

    if not MODEL_PATH.exists():
        logger.warning(f"[HOLDTIME_AI] model not found: {MODEL_PATH}")
        return None

    try:
        _model = joblib.load(MODEL_PATH)
        logger.info("[HOLDTIME_AI] model loaded")
        return _model
    except Exception:
        logger.exception("[HOLDTIME_AI] model load failed")
        _model = None
        return None


# ============================================================
# 内部 util
# ============================================================

def _safe_float(v, default=0.0):
    try:
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# 推論API（外部公開）
# ============================================================

def predict_hold_seconds(features: dict) -> int:
    """
    あと何秒 HOLD するのが期待値的に良いかを返す

    ・EXIT を強制しない（あくまで参考値）
    ・異常値は必ず安全側に倒す

    Parameters
    ----------
    features : dict
        {
            profit_rate,
            drawdown_rate,
            volume_speed,
            volatility,
            trend_strength,
            hold_seconds
        }

    Returns
    -------
    int
        推奨 HOLD 秒（3〜60秒にクリップ）
    """

    model = _load_model()

    # --------------------------------------------------------
    # フォールバック（AI未ロード）
    # --------------------------------------------------------
    if model is None:
        return 10

    try:
        X = [[
            _safe_float(features.get("profit_rate")),
            _safe_float(features.get("drawdown_rate")),
            _safe_float(features.get("volume_speed")),
            _safe_float(features.get("volatility")),
            _safe_float(features.get("trend_strength")),
            _safe_float(features.get("hold_seconds")),
        ]]

        pred = model.predict(X)
        if pred is None or len(pred) == 0:
            return 10

        pred_sec = _safe_float(pred[0], default=10)

        # ----------------------------------------------------
        # 安全クリップ（暴発防止）
        # ----------------------------------------------------
        hold_sec = int(max(3, min(pred_sec, 60)))

        logger.debug(
            f"[HOLDTIME_AI] pred={pred_sec:.2f}s -> clipped={hold_sec}s"
        )

        return hold_sec

    except Exception:
        logger.exception("[HOLDTIME_AI] inference failed")
        return 10
