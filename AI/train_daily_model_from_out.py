# ============================================================
# train_daily_model_from_out.py
# ------------------------------------------------------------
# ・日足CSV（*.T.out.csv）から日足AIを学習
# ・地合い判定用（翌日上昇するか）
# ・直接実行 / python -m 実行の両対応
# ・進捗ログ表示付き
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sys
from pathlib import Path

# ============================================================
# パス解決（直接実行・モジュール実行 両対応）
# ============================================================

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ============================================================
# import（ここから通常）
# ============================================================

import lightgbm as lgb
import joblib

from config.paths import get_path
from AI.csv_loader_daily_from_out import load_daily_all

# ============================================================
# パス設定（paths.py 経由）
# ============================================================

CSV_DIR: Path = get_path("raw_stock_data") / "daily"

MODEL_DIR: Path = get_path("ai_models") / "daily"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH: Path = MODEL_DIR / "model_daily_base.pkl"

# ============================================================
# 学習本体
# ============================================================

def train_daily():
    print("📘 日足AI 学習開始")

    # ----------------------------
    # データ読み込み
    # ----------------------------
    df = load_daily_all(CSV_DIR)

    if df.empty:
        print("❌ 日足データが読み込めませんでした")
        return

    print(f"📊 読み込み行数: {len(df):,}")
    print(f"📈 銘柄数: {df['symbol'].nunique():,}")

    # ----------------------------
    # ラベル作成（翌日上昇）
    # ----------------------------
    df["label"] = (
        df.groupby("symbol")["close"].shift(-1) > df["close"]
    ).astype(int)

    df = df.dropna()

    # ----------------------------
    # 特徴量選択
    # ----------------------------
    features = [
        c for c in df.columns
        if c not in ("symbol", "date", "label")
    ]

    X = df[features]
    y = df["label"]

    print(f"🧪 学習行数: {len(X):,}")
    print(f"🧩 特徴量数: {len(features)}")

    # ----------------------------
    # LightGBM Dataset
    # ----------------------------
    train_data = lgb.Dataset(X, label=y)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbosity": -1,
        "seed": 42,
    }

    print("🚀 LightGBM 学習開始")

    # ----------------------------
    # 学習（進捗ログあり）
    # ----------------------------
    model = lgb.train(
        params,
        train_data,
        num_boost_round=300,
        callbacks=[
            lgb.log_evaluation(period=10)
        ]
    )

    # ----------------------------
    # モデル保存
    # ----------------------------
    joblib.dump(
        {
            "model": model,
            "features": features,
        },
        MODEL_PATH
    )

    print("✅ 日足AI 学習完了")
    print(f"💾 保存先: {MODEL_PATH}")
    print(f"🔢 使用特徴量数: {len(features)}")

# ============================================================
# エントリポイント
# ============================================================

if __name__ == "__main__":
    train_daily()
