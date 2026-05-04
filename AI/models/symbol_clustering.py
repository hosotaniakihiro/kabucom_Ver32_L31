# ============================================================
# symbol_clustering.py
# ------------------------------------------------------------
# ・銘柄特徴量からクラスタIDを作る
# ・月1回 or 四半期で再計算
# ============================================================

from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
import joblib
from pathlib import Path

def run_symbol_clustering(df_features, n_clusters=4):
    X = df_features[
        ["price_level","avg_volume_20","atr_ratio","volatility_std_20"]
    ].fillna(0)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    kmeans = KMeans(
        n_clusters=n_clusters,
        random_state=42,
        n_init=10
    )
    df_features["cluster_id"] = kmeans.fit_predict(X_scaled)

    return df_features, scaler, kmeans


def save_cluster_model(df_features, scaler, kmeans, model_dir: Path):
    model_dir.mkdir(exist_ok=True, parents=True)

    joblib.dump(
        {
            "symbol_cluster": df_features[["symbol","cluster_id"]],
            "scaler": scaler,
            "kmeans": kmeans,
        },
        model_dir / "symbol_cluster.pkl"
    )
