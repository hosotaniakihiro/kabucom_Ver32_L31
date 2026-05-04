# ============================================================
# File   : pj/AI/build_cluster_params.py
# Created: 2026-01-02
# ------------------------------------------------------------
# 市場地合いクラスタ 学習パラメータ生成
# ============================================================

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler


# ============================================================
# 設定
# ============================================================

N_CLUSTERS = 4          # 0:危険 1:弱い 2:通常 3:強い
RANDOM_STATE = 42


# ============================================================
# 特徴量定義
# ============================================================

FEATURE_COLUMNS = [
    "nikkei_change_pct",
    "topix_change_pct",
    "market_volume_ratio",
    "advance_ratio",          # adv / (adv + dec)
    "volatility_index",       # 任意（なければ自動生成）
]


# ============================================================
# 前処理
# ============================================================

def _prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    欠損補完・派生特徴量生成
    """

    df = df.copy()

    # --------------------------------------------------------
    # 必須カラム補完
    # --------------------------------------------------------
    for col in FEATURE_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0

    # --------------------------------------------------------
    # advance_ratio 補完
    # --------------------------------------------------------
    if "advance_ratio" not in df.columns:
        adv = df.get("advance", 0)
        dec = df.get("decline", 0)
        df["advance_ratio"] = adv / np.maximum(adv + dec, 1)

    # --------------------------------------------------------
    # ボラティリティ（簡易）
    # --------------------------------------------------------
    if "volatility_index" not in df.columns:
        ret = df["nikkei_change_pct"].fillna(0)
        df["volatility_index"] = ret.rolling(5, min_periods=1).std().fillna(0)

    # --------------------------------------------------------
    # 無限・欠損処理
    # --------------------------------------------------------
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.fillna(0)

    return df[FEATURE_COLUMNS]


# ============================================================
# メイン：クラスタ学習
# ============================================================

def build_cluster_params(train_df: pd.DataFrame) -> dict:
    """
    市場地合いクラスタを学習し、推論用パラメータを返す

    return:
        {
            "scaler": StandardScaler,
            "model": KMeans,
            "cluster_centers": ndarray,
            "cluster_rank": dict,
            "feature_columns": list
        }
    """

    if train_df is None or train_df.empty:
        raise ValueError("train_df is empty")

    # --------------------------------------------------------
    # 特徴量生成
    # --------------------------------------------------------
    feat_df = _prepare_features(train_df)

    # --------------------------------------------------------
    # 標準化
    # --------------------------------------------------------
    scaler = StandardScaler()
    X = scaler.fit_transform(feat_df.values)

    # --------------------------------------------------------
    # クラスタリング
    # --------------------------------------------------------
    model = KMeans(
        n_clusters=N_CLUSTERS,
        random_state=RANDOM_STATE,
        n_init=20,
    )
    labels = model.fit_predict(X)

    # --------------------------------------------------------
    # クラスタ評価（強弱スコア化）
    # --------------------------------------------------------
    tmp = feat_df.copy()
    tmp["cluster"] = labels

    # 強さスコア（上昇率 + breadth + 出来高）
    strength_score = (
        tmp["nikkei_change_pct"]
        + tmp["topix_change_pct"]
        + tmp["advance_ratio"] * 2
        + tmp["market_volume_ratio"]
    )

    cluster_strength = (
        strength_score
        .groupby(tmp["cluster"])
        .mean()
        .sort_values()
    )

    # 弱い → 強い に 0〜3 を割当
    cluster_rank = {
        cluster_id: rank
        for rank, cluster_id in enumerate(cluster_strength.index)
    }

    # --------------------------------------------------------
    # 返却
    # --------------------------------------------------------
    return {
        "scaler": scaler,
        "model": model,
        "cluster_centers": model.cluster_centers_,
        "cluster_rank": cluster_rank,
        "feature_columns": FEATURE_COLUMNS,
    }


# ============================================================
# 推論用（runtime 側で使用）
# ============================================================

def assign_cluster(features: dict, cluster_params: dict) -> int:
    """
    単発推論用：特徴量 dict → regime (0〜3)
    """

    scaler = cluster_params["scaler"]
    model = cluster_params["model"]
    cluster_rank = cluster_params["cluster_rank"]
    cols = cluster_params["feature_columns"]

    x = np.array([[features.get(c, 0.0) for c in cols]])
    x_scaled = scaler.transform(x)

    cluster_id = model.predict(x_scaled)[0]
    return cluster_rank.get(cluster_id, 2)
