# ============================================================
# train_intraday_interval.py
# ------------------------------------------------------------
# ・分足 / 秒足 を interval 単位で学習
# ・全銘柄混合で 1 モデル
# ・銘柄クラスタリング（cluster_id）を特徴量に使用
# ・LightGBM 2値分類
# ============================================================

import lightgbm as lgb
import joblib
from pathlib import Path
import pandas as pd

from csv_loader_intraday import load_intraday_all
from label_builder_intraday import build_intraday_label

# ============================================================
# パス設定（プロジェクトルート基準）
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSV_DIR = PROJECT_ROOT / "csv_data"
MODEL_DIR = PROJECT_ROOT / "AI" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# 銘柄クラスタモデル
CLUSTER_MODEL_PATH = MODEL_DIR / "symbol_cluster.pkl"

# ============================================================
# 学習関数
# ============================================================

def train_interval(interval: str):
    print(f"\n📘 {interval} 学習開始")

    # --------------------------------------------------------
    # データ読み込み
    # --------------------------------------------------------
    df = load_intraday_all(CSV_DIR, interval)
    if df.empty:
        print("⚠ データなし")
        return

    # --------------------------------------------------------
    # ラベル生成
    # --------------------------------------------------------
    df["label"] = build_intraday_label(df, interval)

    # --------------------------------------------------------
    # 銘柄クラスタ読み込み & JOIN
    # --------------------------------------------------------
    if CLUSTER_MODEL_PATH.exists():
        cluster_data = joblib.load(CLUSTER_MODEL_PATH)
        df_cluster = cluster_data["symbol_cluster"]

        df = df.merge(df_cluster, how="left", on="symbol")
        df["cluster_id"] = df["cluster_id"].fillna(-1)

        print("🔗 銘柄クラスタリング適用")
    else:
        df["cluster_id"] = -1
        print("⚠ symbol_cluster.pkl が無いため cluster_id=-1")

    # --------------------------------------------------------
    # NaN 除外
    # --------------------------------------------------------
    df = df.dropna()

    # --------------------------------------------------------
    # 特徴量
    # --------------------------------------------------------
    features = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "cluster_id",   # ★追加
    ]

    X = df[features]
    y = df["label"]

    print(f"🧪 学習行数: {len(X)}")
    print(f"📊 クラスタ種類数: {X['cluster_id'].nunique()}")

    # --------------------------------------------------------
    # LightGBM 学習
    # --------------------------------------------------------
    train_data = lgb.Dataset(X, label=y)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "num_leaves": 63,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbosity": -1,
        "seed": 42,
    }

    model = lgb.train(
        params,
        train_data,
        num_boost_round=300
    )

    # --------------------------------------------------------
    # モデル保存
    # --------------------------------------------------------
    model_path = MODEL_DIR / f"model_{interval}.pkl"
    joblib.dump(
        {
            "model": model,
            "features": features,
        },
        model_path
    )

    print(f"✅ 保存完了: {model_path.name}")

# ============================================================
# 一括学習エントリポイント
# ============================================================

if __name__ == "__main__":
    for interval in ["60", "5", "3", "2", "1", "10s", "5s", "1s"]:
        train_interval(interval)
