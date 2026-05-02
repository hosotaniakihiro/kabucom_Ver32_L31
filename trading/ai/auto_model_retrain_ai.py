# ============================================================
# trading/ai/auto_model_retrain_ai.py
# PRODUCTION AUTO MODEL RETRAIN ENGINE
#
# Self-learning module for AI trading system
#
# Automatically retrains ML models using trade outcomes
# ============================================================

from __future__ import annotations

import logging
import os
import time
import pandas as pd
import numpy as np
from typing import Dict

from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

import lightgbm as lgb

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_auc(y_true, y_pred):

    try:

        if len(set(y_true)) < 2:
            return 0.5

        return roc_auc_score(y_true, y_pred)

    except Exception:

        return 0.5


# ============================================================
# Auto Retrain Engine
# ============================================================

class AutoModelRetrainAI:

    def __init__(self):

        self.data_path = "data/trade_history.parquet"

        self.model_path = "models/alpha_model.txt"

        self.retrain_interval = 3600  # seconds

        self.min_samples = 200

        self.running = False

    # --------------------------------------------------------
    # start background retrain
    # --------------------------------------------------------

    def start(self):

        if self.running:
            return

        self.running = True

        logger.info("[RETRAIN] engine started")

        while self.running:

            try:

                self._retrain_cycle()

            except Exception:

                logger.exception("Retrain cycle failure")

            time.sleep(self.retrain_interval)

    # --------------------------------------------------------
    # stop engine
    # --------------------------------------------------------

    def stop(self):

        self.running = False

        logger.info("[RETRAIN] engine stopped")

    # --------------------------------------------------------
    # retrain cycle
    # --------------------------------------------------------

    def _retrain_cycle(self):

        if not os.path.exists(self.data_path):

            logger.warning("[RETRAIN] no trade history")

            return

        df = pd.read_parquet(self.data_path)

        if len(df) < self.min_samples:

            logger.info("[RETRAIN] insufficient samples")

            return

        X, y = self._prepare_dataset(df)

        X_train, X_val, y_train, y_val = train_test_split(

            X,
            y,
            test_size=0.2,
            random_state=42

        )

        model = self._train_model(
            X_train,
            y_train
        )

        preds = model.predict(X_val)

        auc = _safe_auc(y_val, preds)

        logger.info("[RETRAIN] validation AUC %.4f", auc)

        if auc > 0.55:

            model.save_model(self.model_path)

            logger.info("[RETRAIN] model updated")

        else:

            logger.info("[RETRAIN] model rejected")

    # --------------------------------------------------------
    # dataset preparation
    # --------------------------------------------------------

    def _prepare_dataset(self, df):

        features = [

            "orderflow_imbalance",
            "micro_momentum",
            "vwap_deviation",
            "spread",
            "volume_acceleration",
            "trade_intensity",
            "liquidity_pressure",
            "volatility",
            "tick_momentum"

        ]

        X = df[features].fillna(0)

        y = (df["pnl"] > 0).astype(int)

        return X, y

    # --------------------------------------------------------
    # train model
    # --------------------------------------------------------

    def _train_model(self, X, y):

        dataset = lgb.Dataset(X, label=y)

        params = {

            "objective": "binary",

            "metric": "auc",

            "learning_rate": 0.05,

            "num_leaves": 31,

            "feature_fraction": 0.8,

            "bagging_fraction": 0.8,

            "bagging_freq": 5,

            "verbosity": -1

        }

        model = lgb.train(

            params,

            dataset,

            num_boost_round=200

        )

        return model


# ============================================================
# Singleton
# ============================================================

_engine = None


def get_auto_model_retrain_ai():

    global _engine

    if _engine is None:

        _engine = AutoModelRetrainAI()

    return _engine