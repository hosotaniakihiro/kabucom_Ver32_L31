# ============================================================
# pj/AI/cluster_params_builder.py
# Ver1.0
# Updated: 2026-01-02
# ------------------------------------------------------------
# クラスタ別最適パラメータ自動生成
# ============================================================

import pandas as pd


def build_cluster_params(df: pd.DataFrame):
    """
    df: 学習CSV（tosama_train.csv）

    return:
        dict[cluster_id] = {
            ai_confidence,
            fast_ret,
            hold_limit_sec
        }
    """

    params = {}

    for cid, g in df.groupby("cluster_id"):

        winners = g[g["label"] == 1]
        if winners.empty:
            continue

        params[cid] = {
            "ai_confidence": winners["ai_confidence"].quantile(0.4),
            "fast_ret": winners["fast_ret"].quantile(0.4),
            "hold_limit_sec": int(winners["hold_seconds"].median()),
        }

    return params
