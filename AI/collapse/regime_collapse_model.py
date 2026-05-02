# ============================================================
# File   : AI/collapse/regime_collapse_model.py
# Version: V1.0-FINAL-REGIME-COLLAPSE-MODEL-LGBM
# ------------------------------------------------------------
# ✔ Regime別モデル管理
# ✔ LightGBM分類器前提
# ✔ predict_proba使用
# ✔ 特徴量順固定
# ✔ 欠損安全処理
# ✔ モデル未存在フェイルセーフ
# ✔ 0〜1出力保証
# ✔ 夜間再ロード対応
# ✔ exit_loop高頻度呼び出し前提
# ============================================================

import os
import joblib
import numpy as np
import logging
from threading import Lock

logger = logging.getLogger(__name__)


class RegimeCollapseModel:
    """
    Regime別 崩壊検知AI

    想定regime例:
        TREND_UP
        RANGE
        PANIC
        LOW_VOL
    """

    def __init__(
        self,
        regime_model_paths: dict,
        feature_order: list[str],
    ):
        """
        regime_model_paths:
            {
                "TREND_UP": "path/to/model.pkl",
                "RANGE": "path/to/model.pkl",
                ...
            }

        feature_order:
            学習時の特徴量順
        """

        self.regime_model_paths = regime_model_paths
        self.feature_order = feature_order

        self.models = {}
        self._lock = Lock()

        self._load_all_models()

    # ============================================================
    # 全モデルロード
    # ============================================================

    def _load_all_models(self):
        for regime, path in self.regime_model_paths.items():

            if not os.path.exists(path):
                logger.warning(
                    f"[RegimeCollapseModel] model not found: {regime} -> {path}"
                )
                self.models[regime] = None
                continue

            try:
                model = joblib.load(path)
                self.models[regime] = model

                logger.info(
                    f"[RegimeCollapseModel] loaded: {regime}"
                )

            except Exception:
                logger.exception(
                    f"[RegimeCollapseModel] load failed: {regime}"
                )
                self.models[regime] = None

    # ============================================================
    # 特徴量整形
    # ============================================================

    def _build_vector(self, feature_dict: dict):

        vector = []

        for col in self.feature_order:
            val = feature_dict.get(col, 0.0)

            if val is None:
                val = 0.0

            try:
                val = float(val)
            except Exception:
                val = 0.0

            if np.isnan(val):
                val = 0.0

            vector.append(val)

        return np.array([vector], dtype=float)

    # ============================================================
    # 推論
    # ============================================================

    def predict(self, regime: str, feature_dict: dict) -> float:
        """
        regimeに対応するモデルで崩壊確率を返す

        戻り値:
            0.0〜1.0
        """

        model = self.models.get(regime)

        if model is None:
            # fallback: モデル無し時は安全側
            return 0.0

        try:
            X = self._build_vector(feature_dict)

            proba = model.predict_proba(X)[0][1]

            # 安全クリップ
            if proba < 0:
                return 0.0
            if proba > 1:
                return 1.0

            return float(proba)

        except Exception:
            logger.exception(
                f"[RegimeCollapseModel] predict failed: {regime}"
            )
            return 0.0

    # ============================================================
    # 再ロード（夜間モデル更新対応）
    # ============================================================

    def reload(self):
        with self._lock:
            self._load_all_models()

    # ============================================================
    # 有効確認
    # ============================================================

    def is_ready(self, regime: str = None) -> bool:
        if regime:
            return self.models.get(regime) is not None

        return any(m is not None for m in self.models.values())