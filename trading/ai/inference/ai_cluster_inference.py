# ============================================================
# File   : trading/ai/inference/ai_cluster_inference.py
# Version: Ver1.0.0-PRO-CLUSTER-LGBM
# ------------------------------------------------------------
# ✔ intraday基準推論
# ✔ クラスタ自動判定
# ✔ モデル自動選択
# ✔ 未存在耐性
# ✔ global_data安全更新
# ✔ scheduler停止防止
# ============================================================

from __future__ import annotations

import os
import time
import logging
import numpy as np
import pandas as pd
import lightgbm as lgb

from global_state import global_data

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.3

MODEL_DIR = os.path.join("AI", "models")

FEATURE_COLUMNS = [
    "ret1",
    "ma5_slope",
    "ma25_slope",
    "volume_z",
    "rank_score",
    "rank_delta",
]


# ------------------------------------------------------------
# モデルキャッシュ
# ------------------------------------------------------------

_model_cache: dict[int, lgb.Booster] = {}


def _load_model(cluster_id: int):

    if cluster_id in _model_cache:
        return _model_cache[cluster_id]

    model_path = os.path.join(
        MODEL_DIR,
        f"cluster_{cluster_id}.txt"
    )

    if not os.path.exists(model_path):
        logger.warning("Model not found: %s", model_path)
        return None

    try:
        model = lgb.Booster(model_file=model_path)
        _model_cache[cluster_id] = model
        return model
    except Exception:
        logger.exception("Model load failed")
        return None


# ------------------------------------------------------------
# クラスタ判定
# ------------------------------------------------------------

def _detect_cluster(row: pd.Series) -> int:

    vol = abs(row.get("ret1", 0))
    vz = row.get("volume_z", 0)
    rank = row.get("rank_score", 0)

    if rank > 8:
        return 3
    if vz > 2:
        return 2
    if vol > 0.005:
        return 1
    return 0


# ============================================================
# メイン推論ループ
# ============================================================

def ai_cluster_inference_loop():

    logger.info("🟢 ai_cluster_inference_loop started")

    while True:

        try:

            feat = getattr(global_data, "feature_cache", None)

            if feat is None or not isinstance(feat, pd.DataFrame):
                time.sleep(POLL_INTERVAL)
                continue

            if feat.empty:
                time.sleep(POLL_INTERVAL)
                continue

            feat = feat.copy()

            ai_probs = []

            for _, row in feat.iterrows():

                cluster = _detect_cluster(row)
                model = _load_model(cluster)

                if model is None:
                    ai_probs.append(0.5)
                    continue

                X = np.array([
                    row.get(col, 0.0)
                    for col in FEATURE_COLUMNS
                ]).reshape(1, -1)

                try:
                    prob = model.predict(X)[0]
                except Exception:
                    prob = 0.5

                ai_probs.append(float(prob))

            feat["ai_prob"] = ai_probs

            try:
                global_data.ai_cache = feat
            except Exception:
                setattr(global_data, "ai_cache", feat)

            logger.debug(
                "[AI] inference done rows=%d",
                len(feat)
            )

        except Exception:
            logger.exception("❌ ai_cluster_inference_loop error")

        time.sleep(POLL_INTERVAL)