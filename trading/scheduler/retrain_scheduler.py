# ============================================================
# File   : trading/scheduler/retrain_scheduler.py
# Version: FINAL-ROBUST-RETRAIN-SCHEDULER
# ------------------------------------------------------------
# ✔ daemon thread
# ✔ 定時再学習
# ✔ モデル保存
# ✔ 例外耐性
# ============================================================

from __future__ import annotations
import threading
import time
import logging
from trading.learning.online_trainer import fetch_training_data, retrain_model
from trading.ai.model_saver import save_model

logger = logging.getLogger(__name__)


def start_retrain_loop(model_manager, interval_seconds=3600):

    def loop():
        logger.info("[RETRAIN] loop started")

        while True:
            try:
                time.sleep(interval_seconds)

                df = fetch_training_data()
                if df is None or df.empty:
                    logger.info("[RETRAIN] no data")
                    continue

                for cluster, model in model_manager.models.items():
                    updated_model = retrain_model(
                        df,
                        model,
                        feature_cols=[
                            c for c in df.columns
                            if c not in ["id", "timestamp", "symbol", "reward"]
                        ],
                    )

                    save_model(updated_model, f"models/{cluster}.pkl")

            except Exception:
                logger.exception("[RETRAIN] loop error")

    t = threading.Thread(target=loop, daemon=True)
    t.start()