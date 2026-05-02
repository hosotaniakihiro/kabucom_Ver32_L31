# ============================================================
# File   : AI/collapse/pre_collapse_model.py
# Version: V1.0-FINAL-PRE-COLLAPSE-MODEL-LGBM
# ------------------------------------------------------------
# ✔ LightGBM分類モデル前提
# ✔ predict_proba使用
# ✔ 特徴量順序固定
# ✔ 欠損安全処理
# ✔ 0〜1出力保証
# ✔ モデル未ロード安全処理
# ✔ exit_loop高頻度呼び出し前提
# ============================================================

import os
import joblib
import numpy as np
import logging
from threading import Lock

logger = logging.getLogger(__name__)


class PreCollapseModel:
    """
    崩壊予兆検知AI

    想定ターゲット:
        60秒以内に -0.8% 下落するか
    """

    def __init__(self, model_path: str, feature_order: list[str]):
        """
        model_path:
            学習済み .pkl パス

        feature_order:
            学習時の特徴量順序（必須）
        """

        self.model_path = model_path
        self.feature_order = feature_order
        self.model = None
        self._lock = Lock()

        self._load_model()

    # ============================================================
    # モデルロード
    # ============================================================

    def _load_model(self):
        if not os.path.exists(self.model_path):
            logger.warning(
                f"[PreCollapseModel] model not found: {self.model_path}"
            )
            return

        try:
            self.model = joblib.load(self.model_path)
            logger.info(
                f"[PreCollapseModel] loaded: {self.model_path}"
            )
        except Exception:
            logger.exception(
                "[PreCollapseModel] model load failed"
            )
            self.model = None

    # ============================================================
    # 特徴量整形
    # ============================================================

    def _build_vector(self, feature_dict: dict):

        vector = []

        for col in self.feature_order:
            val = feature_dict.get(col, 0.0)

            # 欠損対策
            if val is None:
                val = 0.0

            try:
                val = float(val)
            except Exception:
                val = 0.0

            # NaN対策
            if np.isnan(val):
                val = 0.0

            vector.append(val)

        return np.array([vector], dtype=float)

    # ============================================================
    # 推論
    # ============================================================

    def predict(self, feature_dict: dict) -> float:
        """
        戻り値:
            0.0〜1.0（崩壊予兆確率）
        """

        if self.model is None:
            return 0.0

        try:
            X = self._build_vector(feature_dict)

            # LightGBM分類器想定
            proba = self.model.predict_proba(X)[0][1]

            # 安全クリップ
            if proba < 0:
                return 0.0
            if proba > 1:
                return 1.0

            return float(proba)

        except Exception:
            logger.exception("[PreCollapseModel] predict failed")
            return 0.0

    # ============================================================
    # モデル再読込（夜間更新用）
    # ============================================================

    def reload(self):
        with self._lock:
            self._load_model()

    # ============================================================
    # モデル有効確認
    # ============================================================

    def is_ready(self) -> bool:
        return self.model is not None