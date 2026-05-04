# ============================================================
# SELL AI（最終可否判定 / LightGBM 推論専用）
# ------------------------------------------------------------
# ・SELL（利確 / 損切り / トレール）してよいかの最終判断
# ・方式選択は行わない（sell_ai_boost 側の責務）
# ・モデル無し / 例外時は必ず False
# ============================================================

from pathlib import Path
import logging
from typing import Dict, Union

import pandas as pd
import joblib

logger = logging.getLogger(__name__)

# ============================================================
# BASE DIR / MODEL PATH
# ============================================================
BASE_DIR = Path(__file__).resolve().parents[2]
MODEL_PATH = BASE_DIR / "model" / "sell_lgbm.pkl"

# ============================================================
# FEATURES（学習側と完全一致させる）
# ※ 学習時に確定させること
# ============================================================
FEATURE_COLUMNS = [
    "profit_rate",      # 含み益率（%）
    "drawdown_rate",    # 含み損率（%）
    "hold_seconds",     # 保持秒数
    "volume_speed",     # 出来高速度
    "volatility",       # ボラティリティ
    "trend_strength",   # トレンド強度
]

# ============================================================
# 内部：モデル遅延ロード
# ============================================================
_model = None


def _load_model():
    global _model
    if _model is not None:
        return _model

    if not MODEL_PATH.exists():
        logger.warning(f"[SELL LGBM] model not found: {MODEL_PATH}")
        return None

    try:
        _model = joblib.load(MODEL_PATH)
        logger.info("[SELL LGBM] model loaded")
        return _model
    except Exception:
        logger.exception("[SELL LGBM] model load failed")
        _model = None
        return None


# ============================================================
# 入力正規化
# ============================================================
def _build_input_df(features: Union[Dict, pd.DataFrame]) -> pd.DataFrame:
    if isinstance(features, pd.DataFrame):
        df = features.copy()
    else:
        df = pd.DataFrame([features])

    # 欠損特徴量を 0 で補完
    for c in FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = 0

    df = df[FEATURE_COLUMNS]

    # 型安全化
    for c in FEATURE_COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    return df


# ============================================================
# メインAPI
# ============================================================
def predict_sell_ok(
    features: Union[Dict, pd.DataFrame],
    threshold: float = 0.5,
) -> bool:
    """
    SELL 可否の最終判定

    Args:
        features (dict or DataFrame): 特徴量
        threshold (float): SELL 判定閾値

    Returns:
        bool: True = SELL 許可 / False = HOLD
    """

    model = _load_model()
    if model is None:
        return False

    try:
        X = _build_input_df(features)
        prob = float(model.predict_proba(X)[0, 1])

        decision = prob >= threshold

        logger.debug(
            f"[SELL LGBM] prob={prob:.3f} thr={threshold:.2f} decision={decision}"
        )

        return decision

    except Exception:
        logger.exception("[SELL LGBM] prediction failed")
        return False
