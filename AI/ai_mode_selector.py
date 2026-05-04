# ============================================================
# AI/ai_mode_selector.py
# Ver25-FINAL-MODE-SELECTOR
# ------------------------------------------------------------
# ✔ ENTRY方式を AI が決定
# ✔ BREAKOUT / PULLBACK / SKIP を返す
# ✔ 発注は一切しない（判断専用）
# ✔ モデル未ロード時は安全側（SKIP）
# ============================================================

import logging
from pathlib import Path

import joblib
import numpy as np

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

MODEL_DIR = Path("AI")

MODEL_BREAKOUT_PATH = MODEL_DIR / "model_breakout.pkl"
MODEL_PULLBACK_PATH = MODEL_DIR / "model_pullback.pkl"

# ENTRY判定の最低確率
MIN_PROB_BREAKOUT = 0.65
MIN_PROB_PULLBACK = 0.70

# ============================================================
# モデルロード（起動時1回）
# ============================================================

_model_breakout = None
_model_pullback = None

if MODEL_BREAKOUT_PATH.exists():
    try:
        _model_breakout = joblib.load(MODEL_BREAKOUT_PATH)
        logger.info("🧠 breakout model loaded")
    except Exception as e:
        logger.error(f"❌ breakout model load failed: {e}")

if MODEL_PULLBACK_PATH.exists():
    try:
        _model_pullback = joblib.load(MODEL_PULLBACK_PATH)
        logger.info("🧠 pullback model loaded")
    except Exception as e:
        logger.error(f"❌ pullback model load failed: {e}")

# ============================================================
# 特徴量定義
# ※ 学習時と順序を必ず合わせる
# ============================================================

COMMON_FEATURES = [
    "volume_speed",
    "price_velocity",
    "spread",
    "distance_from_vwap",
    "orderbook_imbalance",
    "trend_strength",
]

BREAKOUT_FEATURES = [
    "high_break_distance",
    "range_compression",
    "recent_high_count",
]

PULLBACK_FEATURES = [
    "pullback_depth",
    "vwap_touch_count",
    "ma_support_strength",
]


# ============================================================
# ユーティリティ
# ============================================================

def _safe(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _build_feature_vector(symbol: str, feature_names: list):
    """
    global_data から最新特徴量を取得し、ベクトル化
    """
    feat = []

    src = global_data.latest_features.get(symbol)
    if not src:
        return None

    for k in feature_names:
        feat.append(_safe(src.get(k)))

    return np.array(feat, dtype="float32").reshape(1, -1)


# ============================================================
# メイン：方式決定
# ============================================================

def decide_entry_mode(symbol: str) -> str:
    """
    Returns:
        "BREAKOUT" | "PULLBACK" | "SKIP"
    """

    symbol = str(symbol)

    # --------------------------------------------------------
    # 特徴量がまだ無い → SKIP
    # --------------------------------------------------------
    if symbol not in global_data.latest_features:
        logger.debug(f"SKIP(no features): {symbol}")
        return "SKIP"

    # --------------------------------------------------------
    # モデル未ロード → SKIP（安全側）
    # --------------------------------------------------------
    if _model_breakout is None and _model_pullback is None:
        logger.debug(f"SKIP(no models): {symbol}")
        return "SKIP"

    p_break = -1.0
    p_pull = -1.0

    # --------------------------------------------------------
    # BREAKOUT 確率
    # --------------------------------------------------------
    if _model_breakout is not None:
        x_b = _build_feature_vector(
            symbol, COMMON_FEATURES + BREAKOUT_FEATURES
        )
        if x_b is not None:
            try:
                p_break = float(
                    _model_breakout.predict_proba(x_b)[0][1]
                )
            except Exception as e:
                logger.error(f"breakout predict failed {symbol}: {e}")

    # --------------------------------------------------------
    # PULLBACK 確率
    # --------------------------------------------------------
    if _model_pullback is not None:
        x_p = _build_feature_vector(
            symbol, COMMON_FEATURES + PULLBACK_FEATURES
        )
        if x_p is not None:
            try:
                p_pull = float(
                    _model_pullback.predict_proba(x_p)[0][1]
                )
            except Exception as e:
                logger.error(f"pullback predict failed {symbol}: {e}")

    # --------------------------------------------------------
    # 判定ロジック
    # --------------------------------------------------------
    # 両方低い → SKIP
    if p_break < MIN_PROB_BREAKOUT and p_pull < MIN_PROB_PULLBACK:
        logger.info(
            f"SKIP(low prob): {symbol} "
            f"p_break={p_break:.3f} p_pull={p_pull:.3f}"
        )
        return "SKIP"

    # BREAKOUT 優勢
    if p_break >= p_pull and p_break >= MIN_PROB_BREAKOUT:
        logger.info(
            f"BREAKOUT selected: {symbol} p={p_break:.3f}"
        )
        return "BREAKOUT"

    # PULLBACK 優勢
    if p_pull > p_break and p_pull >= MIN_PROB_PULLBACK:
        logger.info(
            f"PULLBACK selected: {symbol} p={p_pull:.3f}"
        )
        return "PULLBACK"

    # 最後の保険
    logger.info(
        f"SKIP(fallback): {symbol} "
        f"p_break={p_break:.3f} p_pull={p_pull:.3f}"
    )
    return "SKIP"
