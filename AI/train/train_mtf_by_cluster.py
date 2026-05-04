# ============================================================
# AI/train/train_mtf_by_cluster.py
# ------------------------------------------------------------
# ✔ クラスタ別 MTF モデル学習
# ✔ 1M / 3M / 5M 対応
# ✔ predict_mtf と完全互換
# ============================================================

import os
import joblib
import pandas as pd
import lightgbm as lgb
from collections import defaultdict

from global_state import global_data
from AI.train.build_mtf_dataset import build_mtf_dataset  # 既存
from utils.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# 設定
# ============================================================

INTERVALS = [1, 3, 5]
MODEL_BASE_DIR = "AI/models"

LGB_PARAMS = dict(
    objective="regression",
    metric="rmse",
    learning_rate=0.05,
    num_leaves=31,
    min_data_in_leaf=50,
    verbosity=-1,
)

NUM_BOOST_ROUND = 300


# ============================================================
def train_all_clusters():
    """
    全クラスタ × 全 interval の MTF モデルを学習
    """

    # symbol → cluster
    symbol_cluster = global_data.symbol_cluster
    if not symbol_cluster:
        raise RuntimeError("symbol_cluster not found")

    # cluster → symbols
    cluster_map = defaultdict(list)
    for sym, c in symbol_cluster.items():
        cluster_map[int(c)].append(sym)

    logger.info("🧠 MTF CLUSTER TRAIN START clusters=%s", list(cluster_map.keys()))

    for cluster, symbols in cluster_map.items():
        for interval in INTERVALS:
            train_one(cluster, symbols, interval)

    logger.info("✅ MTF CLUSTER TRAIN COMPLETE")


# ============================================================
def train_one(cluster: int, symbols: list[str], interval: int):
    """
    単一クラスタ × interval の学習
    """

    logger.info(
        "🚀 TRAIN cluster=%d interval=%dM symbols=%d",
        cluster,
        interval,
        len(symbols),
    )

    # ========================================================
    # データセット生成（既存ロジックを使用）
    # ========================================================
    df = build_mtf_dataset(
        symbols=symbols,
        interval=interval,
        with_daily=True,
        with_ranking=True,
    )

    if df is None or df.empty:
        logger.warning(
            "⚠ dataset empty cluster=%d interval=%dM", cluster, interval
        )
        return

    # ========================================================
    # target / features
    # ========================================================
    if "target_up" not in df.columns:
        raise RuntimeError("target_up not found in dataset")

    y = df["target_up"]
    X = df.drop(columns=["target_up"])

    features = list(X.columns)

    # ========================================================
    # LightGBM
    # ========================================================
    lgb_train = lgb.Dataset(X, y)

    model = lgb.train(
        params=LGB_PARAMS,
        train_set=lgb_train,
        num_boost_round=NUM_BOOST_ROUND,
    )

    # ========================================================
    # 保存
    # ========================================================
    out_dir = os.path.join(MODEL_BASE_DIR, f"cluster{cluster}")
    os.makedirs(out_dir, exist_ok=True)

    out_path = os.path.join(out_dir, f"model_{interval}M.pkl")

    joblib.dump(
        {
            "model": model,
            "features": features,
            "cluster": cluster,
            "interval": interval,
        },
        out_path,
    )

    logger.info("💾 SAVED %s", out_path)


# ============================================================
if __name__ == "__main__":
    train_all_clusters()
