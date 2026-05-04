# ============================================================
# AI/train/sell/sell_lgbm_trail.py
# SELL TRAIL 判定（推論専用・軽量ラッパ）
# ------------------------------------------------------------
# ・TRAIL 中に「降りてよいか？」を判定
# ・False Positive（早降り）を極小化
# ・exit_controller から安全に呼ばれる前提
# ============================================================

from pathlib import Path
import joblib
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# ============================================================
# MODEL PATH
# ============================================================
MODEL_PATH = Path("AI/model/sell_lgbm_trail.pkl")

_model = None


# ============================================================
# 内部：モデル遅延ロード
# ============================================================
def _load_model():
    global _model
    if _model is None:
        if MODEL_PATH.exists():
            try:
                _model = joblib.load(MODEL_PATH)
                logger.info(f"[SELL_TRAIL_AI] model loaded: {MODEL_PATH}")
            except Exception:
                logger.exception("[SELL_TRAIL_AI] model load failed")
                _model = None
        else:
            logger.warning(f"[SELL_TRAIL_AI] model not found: {MODEL_PATH}")
    return _model


# ============================================================
# メインAPI：TRAIL終了して SELL してよいか？
# ============================================================
def predict_sell_trail(
    features: dict,
    threshold: float = 0.75,
) -> bool:
    """
    TRAIL を終了して SELL してよいかを判定する

    Parameters
    ----------
    features : dict
        {
            "profit_rate": float,      # 含み益率 [%]
            "trend_strength": float,   # トレンド強度
            "volatility": float,       # ボラティリティ
            "hold_seconds": int,       # 保持秒数
        }

    threshold : float
        判定閾値（default=0.75）
        ※ 高め推奨（早降り防止）

    Returns
    -------
    bool
        True  : SELL してよい
        False : まだ HOLD（伸ばす）
    """

    model = _load_model()
    if model is None:
        # モデル未ロード時は必ず HOLD（安全側）
        return False

    # --------------------------------------------------------
    # 特徴量整形（完全防御）
    # --------------------------------------------------------
    X = pd.DataFrame([{
        "profit_rate": float(features.get("profit_rate", 0.0)),
        "trend_strength": float(features.get("trend_strength", 0.0)),
        "volatility": float(features.get("volatility", 0.0)),
        "hold_seconds": int(features.get("hold_seconds", 0)),
    }])

    # --------------------------------------------------------
    # 推論
    # --------------------------------------------------------
    try:
        prob = float(model.predict_proba(X)[0, 1])
    except Exception:
        logger.exception("[SELL_TRAIL_AI] inference failed")
        return False

    # --------------------------------------------------------
    # 判定
    # --------------------------------------------------------
    decision = prob >= threshold

    logger.debug(
        f"[SELL_TRAIL_AI] prob={prob:.3f} threshold={threshold:.2f} decision={decision}"
    )

    return decision
