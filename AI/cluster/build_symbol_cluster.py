import pandas as pd
from sklearn.cluster import KMeans
from pathlib import Path

DATA = Path("AI/train/sell/sell_train.csv")
OUT = Path("AI/cluster/symbol_cluster.csv")

FEATURES = [
    "avg_volume_speed",
    "avg_volatility",
    "avg_trend_strength",
]

df = pd.read_csv(DATA)

X = df.groupby("symbol")[FEATURES].mean().dropna()

kmeans = KMeans(n_clusters=6, random_state=42)
clusters = kmeans.fit_predict(X)

out = X.copy()
out["cluster_id"] = clusters
out.reset_index().to_csv(OUT, index=False)

print("✅ symbol cluster created")
