# ============================================================
# File   : AI/collapse_model.py
# Version: V31-FINAL-COLLAPSE-LONG-SHORT-BATCH-SAFE
# ------------------------------------------------------------
# ✔ Long / Short 両対応
# ✔ LightGBM前提（将来差替可能）
# ✔ NaN完全吸収
# ✔ バッチ推論対応
# ✔ 例外安全
# ✔ 動的閾値サポート
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import joblib
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 動的閾値
# ============================================================

def dynamic_collapse_threshold(regime: int) -> float:
    """
    regime:
        0 = TREND_UP
        1 = TREND_DOWN
        2 = RANGE
        3 = VOLATILE
        4 = CRASH
    """

    table = {
        0: 0.75,  # 上昇相場は粘る
        1: 0.55,
        2: 0.60,
        3: 0.50,
        4: 0.40,  # クラッシュは即逃げ
    }

    return table.get(regime, 0.60)


# ============================================================
# CollapseModel
# ============================================================

class CollapseModel:
    """
    collapse確率推定モデル

    目的:
        P(短時間内に -X% 下落)
    """

    def __init__(
        self,
        long_model_path: str,
        short_model_path: Optional[str] = None,
    ):
        self.long_model = self._load_model(long_model_path)
        self.short_model = (
            self._load_model(short_model_path)
            if short_model_path
            else None
        )

        # 使用する特徴順（固定）
        self.feature_order = [
            "ma75_slope",
            "ranking_delta",
            "ranking_persistence",
            "mfe_from_peak",
            "volume_decay",
            "spread_expansion",
            "atr_ratio",
            "regime",
        ]

    # ========================================================
    # Public API
    # ========================================================

    def predict_proba(
        self,
        features: Dict,
        side: str = "LONG"
    ) -> float:

        try:
            X = self._build_array(features)

            if side == "SHORT" and self.short_model:
                prob = self.short_model.predict_proba(X)[0][1]
            else:
                prob = self.long_model.predict_proba(X)[0][1]

            return float(prob)

        except Exception:
            logger.exception("CollapseModel.predict_proba failed")
            return 0.0  # 安全側

    # ========================================================
    # バッチ推論（高速化用）
    # ========================================================

    def predict_batch(
        self,
        feature_list: List[Dict],
        side: str = "LONG"
    ) -> List[float]:

        try:
            X = np.vstack([self._build_array(f) for f in feature_list])

            if side == "SHORT" and self.short_model:
                probs = self.short_model.predict_proba(X)[:, 1]
            else:
                probs = self.long_model.predict_proba(X)[:, 1]

            return probs.tolist()

        except Exception:
            logger.exception("CollapseModel.predict_batch failed")
            return [0.0] * len(feature_list)

    # ========================================================
    # 内部処理
    # ========================================================

    def _build_array(self, features: Dict) -> np.ndarray:

        values = []

        for key in self.feature_order:
            v = features.get(key, 0.0)

            # NaN吸収
            if v is None or (isinstance(v, float) and np.isnan(v)):
                v = 0.0

            values.append(float(v))

        return np.array(values, dtype=np.float32).reshape(1, -1)

    def _load_model(self, path: str):

        try:
            model = joblib.load(path)
            return model
        except Exception:
            logger.exception(f"Failed to load collapse model: {path}")
            raise


# ============================================================
# Optional: スコア統合関数
# ============================================================

def collapse_decision(
    collapse_prob: float,
    regime: int
) -> bool:
    """
    collapse確率 + regime に基づく即時EXIT判定
    """

    threshold = dynamic_collapse_threshold(regime)

    return collapse_prob > threshold