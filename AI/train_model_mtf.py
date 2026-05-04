# ============================================================
# train_model_mtf.py（Ranking Features Integrated Ver）
# ------------------------------------------------------------
# ・5分足 MTF 学習データにランキング特徴量を統合
# ・build_summary_5min_dataset() で作った X, y に df_rank を merge
# ・モデルと特徴量一覧を model_summary5min.pkl に保存
# ============================================================

from build_mtf_5min_summary import build_summary_5min_dataset
from AI.ranking_features import load_ranking_features   # ★追加：ランキング特徴量
import lightgbm as lgb
import pandas as pd
import joblib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = PROJECT_ROOT / "AI" / "models" / "model_summary5min.pkl"


def train_model_mtf():
    print("📘 PUSHサマリー5分足データセット読み込み中...")
    X, y, features = build_summary_5min_dataset()

    # ----------------------------------------
    # ★ ランキング特徴量の読み込み
    # ----------------------------------------
    print("📘 ランキング特徴量読み込み中...")
    df_rank = load_ranking_features()

    if df_rank is None or df_rank.empty:
        print("⚠ ランキング特徴量なし → 通常学習に切替")
    else:
        print(f"📊 ランキング特徴量 {len(df_rank)} 行を結合します")

        # X が numpy の場合 → DataFrame に変換
        if not isinstance(X, pd.DataFrame):
            X = pd.DataFrame(X, columns=features)

        # 日付型を揃える
        if "date" in X.columns:
            X["date"] = pd.to_datetime(X["date"], errors="coerce")

        df_rank["date"] = pd.to_datetime(df_rank["date"], errors="coerce")

        # symbol + date 結合
        if "symbol" in X.columns and "date" in X.columns:
            X = X.merge(df_rank, how="left", on=["symbol", "date"])
        else:
            print("⚠ X に symbol/date が無いためランキング結合できません")

        # 特徴量一覧を更新
        new_rank_features = [c for c in X.columns if c not in features]
        features = list(X.columns)

        print(f"🆕 追加されたランキング特徴量数: {len(new_rank_features)}")
        print(f"📈 総特徴量数: {len(features)}")

    # ----------------------------------------
    # LightGBM 学習
    # ----------------------------------------
    print("🚀 LightGBM 学習開始...")
    train_data = lgb.Dataset(X, label=y)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.03,
        "num_leaves": 48,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
    }

    model = lgb.train(params, train_data, num_boost_round=250)

    # ----------------------------------------
    # モデル保存
    # ----------------------------------------
    joblib.dump({"model": model, "features": features}, MODEL_PATH)
    print(f"✅ 保存完了: {MODEL_PATH}")
    print(f"🔢 特徴量数: {len(features)}")


if __name__ == "__main__":
    train_model_mtf()
