# ============================================================
# AI/train/sell/sell_lgbm_tp.py
# SELL TAKE_PROFIT 判定（推論専用・軽量ラッパ）
# ------------------------------------------------------------
# ・利確してよいか？を判定
# ・precision 重視（早すぎ利確を防ぐ）
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
MODEL_PATH = Path("AI/model/sell_lgbm_tp.pkl")

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
                logger.info(f"[SELL_TP_AI] model loaded: {MODEL_PATH}")
            except Exception:
                logger.exception("[SELL_TP_AI] model load failed")
                _model = None
        else:
            logger.warning(f"[SELL_TP_AI] model not found: {MODEL_PATH}")
    return _model


# ============================================================
# メインAPI：利確して SELL してよいか？
# ============================================================
def predict_sell_tp(
    features: dict,
    threshold: float = 0.60,
) -> bool:
    """
    TAKE_PROFIT を実行してよいかを判定する

    Parameters
    ----------
    features : dict
        {
            "profit_rate": float,      # 含み益率 [%]
            "hold_seconds": int,       # 保持秒数
            "trend_strength": float,   # トレンド強度
            "volatility": float,       # ボラティリティ
        }

    threshold : float
        判定閾値（default=0.60）
        ※ precision 重視のため中〜高め推奨

    Returns
    -------
    bool
        True  : 利確してよい
        False : HOLD（まだ伸ばす）
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
        "hold_seconds": int(features.get("hold_seconds", 0)),
        "trend_strength": float(features.get("trend_strength", 0.0)),
        "volatility": float(features.get("volatility", 0.0)),
    }])

    # --------------------------------------------------------
    # 推論
    # --------------------------------------------------------
    try:
        prob = float(model.predict_proba(X)[0, 1])
    except Exception:
        logger.exception("[SELL_TP_AI] inference failed")
        return False

    # --------------------------------------------------------
    # 判定
    # --------------------------------------------------------
    decision = prob >= threshold

    logger.debug(
        f"[SELL_TP_AI] prob={prob:.3f} threshold={threshold:.2f} decision={decision}"
    )

    return decision
