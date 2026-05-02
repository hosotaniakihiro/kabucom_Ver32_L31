# ============================================================
# File   : trading/ai/collapse_detector.py
# Version: FINAL-AUTONOMOUS-COLLAPSE-DETECTOR
# ------------------------------------------------------------
# ✔ BUY / SELL 両対応
# ✔ NaN / inf / object 完全耐性
# ✔ rule + ML ハイブリッド設計
# ✔ collapse確率 0〜1 強制保証
# ✔ deterministic
# ✔ 学習モデル未ロードでも安全動作
# ✔ 将来拡張可能
# ============================================================

from __future__ import annotations
import numpy as np
import pandas as pd
import logging
import math

logger = logging.getLogger(__name__)


# ============================================================
# 安全数値変換
# ============================================================

def _safe(v, default=0.0):
    try:
        if v is None:
            return default
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _safe_series(x):
    return (
        pd.to_numeric(x, errors="coerce")
        .replace([np.inf, -np.inf], 0.0)
        .fillna(0.0)
    )


# ============================================================
# CollapseDetector
# ============================================================

class CollapseDetector:
    """
    崩壊検知モデル（rule + ML）
    """

    def __init__(self, model=None):
        self.model = model  # 任意MLモデル

    # ========================================================
    # Rule-based collapse判定
    # ========================================================

    def _rule_score(self, features: dict, side: str) -> float:

        slope = _safe(features.get("ma75_slope"))
        rsi = _safe(features.get("rsi"))
        pnl = _safe(features.get("pnl"))
        vol_slope = _safe(features.get("volume_slope"))
        vwap_dev = _safe(features.get("vwap_deviation"))
        atr = _safe(features.get("atr"))

        score = 0.0

        # ----------------------------------------------------
        # BUYポジ崩壊
        # ----------------------------------------------------
        if side == "BUY":

            if slope < -0.002:
                score += 2

            if rsi < 45:
                score += 1.5

            if pnl < -atr * 0.5:
                score += 2.5

            if vol_slope > 0 and slope < 0:
                score += 1.5

            if vwap_dev < -0.01:
                score += 1.0

        # ----------------------------------------------------
        # SELLポジ崩壊
        # ----------------------------------------------------
        else:

            if slope > 0.002:
                score += 2

            if rsi > 55:
                score += 1.5

            if pnl < -atr * 0.5:
                score += 2.5

            if vol_slope > 0 and slope > 0:
                score += 1.5

            if vwap_dev > 0.01:
                score += 1.0

        return score

    # ========================================================
    # ML collapse確率
    # ========================================================

    def _ml_probability(self, features: dict) -> float:

        if self.model is None:
            return 0.0

        try:
            X = np.array(
                [[_safe(v) for v in features.values()]],
                dtype=np.float32
            )

            prob = self.model.predict_proba(X)

            if isinstance(prob, (list, np.ndarray)):
                prob = prob[0]

            return _safe(prob)

        except Exception:
            logger.exception("[COLLAPSE_ML_ERROR]")
            return 0.0

    # ========================================================
    # 公開API
    # ========================================================

    def predict_proba(self, features: dict, side: str = "BUY") -> float:
        """
        collapse確率 0.0〜1.0
        """

        try:

            # Rule score
            rule_score = self._rule_score(features, side)

            # 正規化
            rule_prob = min(1.0, rule_score / 8.0)

            # ML
            ml_prob = self._ml_probability(features)

            # ハイブリッド
            prob = rule_prob * 0.6 + ml_prob * 0.4

            # 強制範囲制限
            prob = _safe(prob)
            prob = max(0.0, min(1.0, prob))

            return prob

        except Exception:
            logger.exception("[COLLAPSE_PREDICT_ERROR]")
            return 0.0