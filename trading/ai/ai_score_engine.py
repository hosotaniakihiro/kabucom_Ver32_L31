# ============================================================
# File   : trading/ai/ai_score_engine.py
# Version: FINAL-ROBUST-AI-SCORE-ENGINE
# ------------------------------------------------------------
# ✔ cluster別モデル適用
# ✔ 数値列のみ使用
# ✔ NaN耐性
# ✔ モデル未存在安全処理
# ✔ ログ出力
# ============================================================

from __future__ import annotations
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def apply_ai_model(
    df: pd.DataFrame,
    model_dict: dict | None = None,
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()
    df["score_ai"] = 0.0

    if model_dict is None:
        return df

    numeric_cols = df.select_dtypes(include=[np.number]).columns

    if len(numeric_cols) == 0:
        return df

    for idx, row in df.iterrows():

        cluster = row.get("cluster")
        model = model_dict.get(cluster)

        if model is None:
            continue

        try:
            features = (
                row[numeric_cols]
                .fillna(0)
                .values
                .reshape(1, -1)
            )

            pred = model.predict(features)

            if isinstance(pred, (list, np.ndarray)):
                pred = float(pred[0])

            df.at[idx, "score_ai"] = float(pred)

        except Exception as e:
            logger.exception(
                "[AI_SCORE] failed idx=%s cluster=%s",
                idx,
                cluster,
            )
            df.at[idx, "score_ai"] = 0.0

    return df