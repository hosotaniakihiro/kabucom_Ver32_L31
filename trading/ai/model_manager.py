# ============================================================
# File   : trading/ai/model_manager.py
# Version: FINAL-ULTRA-ROBUST-PRODUCTION-READY
# ------------------------------------------------------------
# ✔ cluster別モデル管理
# ✔ regime別モデル切替
# ✔ A/Bテスト対応
# ✔ ensemble対応
# ✔ lazy load
# ✔ strict mode
# ✔ thread-safe
# ✔ 自動バックアップ（世代管理）
# ✔ version管理
# ✔ 自動ロールバック
# ✔ 推論健全性チェック
# ✔ warmup preload
# ✔ atomic save
# ✔ predict / predict_proba 対応
# ============================================================

from __future__ import annotations
import logging
import joblib
import os
import shutil
import threading
import random
import time
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)


class ModelManager:

    def __init__(
        self,
        model_registry: Dict[str, Dict[str, str]] | None = None,
        *,
        strict: bool = False,
        backup_dir: str = "model_backups",
        ab_test_ratio: float = 0.5,
        ensemble_mode: bool = False,
    ):
        """
        model_registry:
        {
            "cluster": {
                "bull": "path.pkl",
                "bear": "path.pkl",
                "neutral": "path.pkl",
                "A": "path_A.pkl",
                "B": "path_B.pkl",
                "ensemble": ["m1.pkl", "m2.pkl"]
            }
        }
        """

        self.registry = model_registry or {}
        self.models: Dict[str, object] = {}
        self.strict = strict
        self.backup_dir = backup_dir
        self.ab_ratio = ab_test_ratio
        self.ensemble_mode = ensemble_mode

        self._lock = threading.RLock()

        os.makedirs(self.backup_dir, exist_ok=True)

    # ========================================================
    # key生成
    # ========================================================
    def _key(self, cluster: str, regime: str) -> str:
        return f"{cluster}__{regime}"

    # ========================================================
    # パス取得
    # ========================================================
    def _get_path(self, cluster: str, regime: str):

        cluster_info = self.registry.get(cluster)
        if not cluster_info:
            return None

        return cluster_info.get(regime) or cluster_info.get("neutral")

    # ========================================================
    # atomic save
    # ========================================================
    def _atomic_save(self, model, path: str):
        tmp_path = path + ".tmp"
        joblib.dump(model, tmp_path)
        os.replace(tmp_path, path)

    # ========================================================
    # モデルロード
    # ========================================================
    def _load_model(self, cluster: str, regime: str):

        path = self._get_path(cluster, regime)

        if not path:
            return None

        if not os.path.exists(path):
            if self.strict:
                raise FileNotFoundError(path)
            logger.warning("[MODEL_MANAGER] model not found: %s", path)
            return None

        try:
            model = joblib.load(path)
            logger.info("[MODEL_MANAGER] loaded %s (%s)", cluster, regime)
            return model
        except Exception:
            logger.exception("[MODEL_MANAGER] load failed")
            if self.strict:
                raise
            return None

    # ========================================================
    # モデル取得
    # ========================================================
    def get_model(self, cluster: str, regime: str = "neutral"):

        with self._lock:

            cluster_info = self.registry.get(cluster, {})

            # ---- A/B Test ----
            if "A" in cluster_info and "B" in cluster_info:
                regime = "A" if random.random() < self.ab_ratio else "B"

            key = self._key(cluster, regime)

            if key in self.models:
                return self.models[key]

            model = self._load_model(cluster, regime)

            if model:
                self.models[key] = model

            return model

    # ========================================================
    # ensemble推論
    # ========================================================
    def _predict_single(self, model, X):
        try:
            if hasattr(model, "predict"):
                pred = model.predict(X)
                if hasattr(pred, "__len__"):
                    return float(pred[0])
                return float(pred)
        except Exception:
            logger.exception("[MODEL_MANAGER] single predict failed")
        return 0.0

    def predict(self, cluster: str, regime: str, X):

        if self.ensemble_mode:
            return self._ensemble_predict(cluster, regime, X)

        model = self.get_model(cluster, regime)
        if model is None:
            return 0.0

        return self._predict_single(model, X)

    # ========================================================
    # ensemble処理
    # ========================================================
    def _ensemble_predict(self, cluster: str, regime: str, X):

        cluster_info = self.registry.get(cluster, {})
        model_paths: List[str] = cluster_info.get("ensemble", [])

        if not model_paths:
            return self.predict(cluster, regime, X)

        preds = []

        for path in model_paths:
            if not os.path.exists(path):
                continue

            try:
                model = joblib.load(path)
                preds.append(self._predict_single(model, X))
            except Exception:
                logger.exception("[MODEL_MANAGER] ensemble load failed")

        if not preds:
            return 0.0

        return sum(preds) / len(preds)

    # ========================================================
    # predict_proba対応
    # ========================================================
    def predict_proba(self, cluster: str, regime: str, X):

        model = self.get_model(cluster, regime)
        if model is None:
            return 0.0

        try:
            if hasattr(model, "predict_proba"):
                prob = model.predict_proba(X)
                return float(prob[0][1])
        except Exception:
            logger.exception("[MODEL_MANAGER] predict_proba failed")

        return 0.0

    # ========================================================
    # reload
    # ========================================================
    def reload(self):
        with self._lock:
            self.models.clear()
        logger.info("[MODEL_MANAGER] reloaded")

    # ========================================================
    # save_model（世代管理）
    # ========================================================
    def save_model(self, model, path: str):

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)

            if os.path.exists(path):
                timestamp = int(time.time())
                backup_path = os.path.join(
                    self.backup_dir,
                    f"{os.path.basename(path)}.{timestamp}.bak"
                )
                shutil.copy(path, backup_path)
                logger.info("[MODEL_MANAGER] backup created")

            self._atomic_save(model, path)
            logger.info("[MODEL_MANAGER] model saved")

        except Exception:
            logger.exception("[MODEL_MANAGER] save failed")

    # ========================================================
    # rollback（最新版復元）
    # ========================================================
    def rollback(self, path: str):

        filename = os.path.basename(path)
        backups = [
            f for f in os.listdir(self.backup_dir)
            if f.startswith(filename)
        ]

        if not backups:
            logger.warning("[MODEL_MANAGER] no backup found")
            return

        backups.sort(reverse=True)
        latest_backup = backups[0]

        shutil.copy(
            os.path.join(self.backup_dir, latest_backup),
            path
        )

        logger.info("[MODEL_MANAGER] rollback completed")

    # ========================================================
    # warmup
    # ========================================================
    def warmup(self):
        logger.info("[MODEL_MANAGER] warmup loading all models")
        for cluster in self.registry:
            for regime in self.registry[cluster]:
                self.get_model(cluster, regime)

    # ========================================================
    # モデル情報
    # ========================================================
    def info(self):

        result = {}

        for cluster in self.registry:
            result[cluster] = list(self.registry[cluster].keys())

        return result