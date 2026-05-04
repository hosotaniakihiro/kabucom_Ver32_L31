import pandas as pd
from sklearn.cluster import KMeans
from pathlib import Path

DATA_PATH = Path("AI/train/tosama_train.csv")
OUT_PATH  = Path("AI/config/tonosama_cluster.csv")

FEATURES = [
    "volume_speed",
    "fast_ret",
    "rank_position",
    "price",
    "spread",
    "entry_second",
]

def main():
    df = pd.read_csv(DATA_PATH)
    df = df.dropna(subset=FEATURES)

    X = df[FEATURES]

    kmeans = KMeans(
        n_clusters=6,
        random_state=42,
        n_init=10,
    )

    df["cluster"] = kmeans.fit_predict(X)
    df.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")

    print("saved:", OUT_PATH)

if __name__ == "__main__":
    main()
