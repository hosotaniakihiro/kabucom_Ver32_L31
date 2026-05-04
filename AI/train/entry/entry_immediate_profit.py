# ============================================================
# AI/train/entry/entry_immediate_profit.py
# ------------------------------------------------------------
# 即益AI（ENTRY直後に含み益になる確率）
# ------------------------------------------------------------
# ✔ LightGBM モデルが存在すれば AI 推論
# ✔ 無ければ安全なルールベースへフォールバック
# ✔ 本番耐性最優先（例外・NaN 完全防止）
# ============================================================

from pathlib import Path
import logging
import math

import pandas as pd
import joblib

logger = logging.getLogger(__name__)

# ============================================================
# モデルパス
# ============================================================
BASE_DIR = Path(__file__).resolve().parents[2]
MODEL_PATH = BASE_DIR / "model_entry_immediate_profit.pkl"

# ============================================================
# 使用特徴量（学習と完全一致）
# ============================================================
FEATURE_COLUMNS = [
    "volume_speed",
    "price_velocity",
    "spread",
    "distance_from_vwap",
    "breakout_strength",
    "orderbook_imbalance",
]

# ============================================================
# モデルキャッシュ（★重要）
# ============================================================
_MODEL = None


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    if not MODEL_PATH.exists():
        return None

    try:
        _MODEL = joblib.load(MODEL_PATH)
        logger.info("✅ Immediate profit model loaded")
        return _MODEL
    except Exception:
        logger.exception("❌ Failed to load immediate profit model")
        _MODEL = None
        return None


# ============================================================
# ルールベース（安全フォールバック）
# ============================================================
def _rule_based_score(features: dict) -> float:
    """
    AI が使えない場合の安全スコア
    ※ 0.0〜0.95 に必ず収める
    """
    score = 0.5

    vol = float(features.get("volume_speed", 0) or 0)
    if vol > 8000:
        score += 0.15
    elif vol > 4000:
        score += 0.05

    breakout = float(features.get("breakout_strength", 0) or 0)
    if breakout > 0.3:
        score += 0.15
    elif breakout > 0.15:
        score += 0.05

    velocity = float(features.get("price_velocity", 0) or 0)
    if velocity > 0.3:
        score += 0.10

    score = max(0.0, min(score, 0.95))
    return score


# ============================================================
# メインAPI
# ============================================================
def predict_immediate_profit(features: dict) -> float:
    """
    ENTRY直後（数秒〜数十秒）で
    含み益になる確率を返す

    Args:
        features (dict): 特徴量

    Returns:
        float: 0.0〜1.0（有限値保証）
    """

    if not isinstance(features, dict) or not features:
        return 0.0

    # --------------------------------------------------------
    # モデル取得（キャッシュ）
    # --------------------------------------------------------
    model = _load_model()
    if model is None:
        score = _rule_based_score(features)
        logger.debug("[IMMEDIATE AI][RULE] score=%.3f", score)
        return score

    try:
        # --------------------------------------------
        # 特徴量整形（欠損完全耐性）
        # --------------------------------------------
        row = {}
        for c in FEATURE_COLUMNS:
            v = features.get(c, 0)
            try:
                fv = float(v)
                if not math.isfinite(fv):
                    fv = 0.0
            except Exception:
                fv = 0.0
            row[c] = fv

        X = pd.DataFrame([row])

        prob = float(model.predict_proba(X)[0, 1])

        # 異常値ガード
        if not math.isfinite(prob):
            raise ValueError("prob is not finite")

        prob = max(0.0, min(prob, 0.99))

        logger.debug("[IMMEDIATE AI][LGBM] prob=%.3f", prob)
        return prob

    except Exception:
        # --------------------------------------------
        # 例外時は必ずフォールバック
        # --------------------------------------------
        logger.exception("❌ Immediate profit AI failed, fallback to rule")
        return _rule_based_score(features)
