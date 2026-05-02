# ============================================================
# File   : trading/tick/tick_collapse_detector.py
# Version: V1.0-FINAL-TICK-COLLAPSE-DETECTOR-LOWLATENCY
# ------------------------------------------------------------
# ✔ tick_feature_builder統合
# ✔ 下方向専用collapse強化
# ✔ 瞬間急落即反応
# ✔ VWAP割れ強化
# ✔ spread急拡大反応
# ✔ 出来高加速反応
# ✔ downside圧力統合
# ✔ 0〜1正規化スコア出力
# ✔ exit_loop高速呼び出し前提
# ============================================================

import logging
from trading.tick.tick_feature_builder import build_tick_features

logger = logging.getLogger(__name__)


# ============================================================
# パラメータ（後でBandit連動可能）
# ============================================================

WEIGHTS = {
    "ret_1s": 0.30,
    "ret_3s": 0.20,
    "spread_jump": 0.10,
    "volume_accel": 0.10,
    "vwap_dev": 0.10,
    "downside_pressure": 0.15,
    "tick_speed": 0.05,
}


# ============================================================
# 正規化ユーティリティ
# ============================================================

def _clip01(x):
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return float(x)


def _scale_abs(x, threshold):
    """
    thresholdを超えると1.0に近づく
    """
    return _clip01(abs(x) / threshold)


# ============================================================
# メインcollapse検知
# ============================================================

def detect_tick_collapse(ticks):
    """
    入力: TickStateCache.get_all(symbol)

    出力:
        collapse_strength (0.0〜1.0)
    """

    try:
        features = build_tick_features(ticks)

        if not features:
            return 0.0

        score = 0.0

        # ====================================================
        # ① 短期急落（最重要）
        # ====================================================
        if features["ret_1s"] < 0:
            score += WEIGHTS["ret_1s"] * _scale_abs(
                features["ret_1s"], threshold=0.003  # -0.3%
            )

        if features["ret_3s"] < 0:
            score += WEIGHTS["ret_3s"] * _scale_abs(
                features["ret_3s"], threshold=0.006  # -0.6%
            )

        # ====================================================
        # ② spread急拡大
        # ====================================================
        score += WEIGHTS["spread_jump"] * _clip01(
            features["spread_jump"] / 3.0
        )

        # ====================================================
        # ③ 出来高加速
        # ====================================================
        score += WEIGHTS["volume_accel"] * _clip01(
            features["volume_accel"]
        )

        # ====================================================
        # ④ VWAP割れ（下方向のみ）
        # ====================================================
        if features["vwap_dev"] < 0:
            score += WEIGHTS["vwap_dev"] * _scale_abs(
                features["vwap_dev"], threshold=0.002
            )

        # ====================================================
        # ⑤ downside圧力
        # ====================================================
        score += WEIGHTS["downside_pressure"] * _clip01(
            features["downside_pressure"] / 0.01
        )

        # ====================================================
        # ⑥ tick速度（急変）
        # ====================================================
        score += WEIGHTS["tick_speed"] * _clip01(
            features["tick_speed"] / 0.5
        )

        # ====================================================
        # 最終正規化
        # ====================================================
        collapse_strength = _clip01(score)

        return collapse_strength

    except Exception:
        logger.exception("[TickCollapseDetector] detect failed")
        return 0.0