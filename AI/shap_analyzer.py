# ============================================================
# File: AI/shap_analyzer.py
# ------------------------------------------------------------
# 殿様イナゴ（BUY）専用 SHAP 解析
#
# ✔ 学習済み LightGBM モデルを使用
# ✔ 学習CSV（tonosama_train.csv）を入力
# ✔ 特徴量の寄与度を可視化
# ✔ 閾値調整・特徴量削減の判断材料専用
# ✔ 本番ロジックに一切影響なし
# ============================================================

from __future__ import annotations

import os
import lightgbm as lgb
import pandas as pd
import shap
import matplotlib.pyplot as plt


# ============================================================
# 設定（学習コードと完全一致）
# ============================================================

TRAIN_CSV = os.environ.get(
    "TONOSAMA_TRAIN_CSV",
    "tonosama_train.csv"
)

MODEL_PATH = os.environ.get(
    "TONOSAMA_MODEL_PATH",
    "tonosama_lgbm.txt"
)

# ★ 学習時・ENTRY時と「順序含めて」完全一致させる
FEATURES = [
    "price_velocity",
    "volume_speed",
    "rank_jump",
    "rank_strength",
    "dominant_ratio",
    "spread_ratio",
    "minute_from_open",
]


# ============================================================
# メイン処理
# ============================================================

def main() -> None:
    # --------------------------------------------------------
    # 前提チェック
    # --------------------------------------------------------
    if not os.path.exists(TRAIN_CSV):
        raise FileNotFoundError(f"train csv not found: {TRAIN_CSV}")

    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"model not found: {MODEL_PATH}")

    # --------------------------------------------------------
    # データ読み込み
    # --------------------------------------------------------
    df = pd.read_csv(TRAIN_CSV)

    missing = [c for c in FEATURES if c not in df.columns]
    if missing:
        raise ValueError(f"missing feature columns: {missing}")

    X = df[FEATURES].copy()

    # NaN / inf 安全対策
    X = X.replace([float("inf"), float("-inf")], 0.0)
    X = X.fillna(0.0)

    # --------------------------------------------------------
    # モデルロード
    # --------------------------------------------------------
    model = lgb.Booster(model_file=MODEL_PATH)

    # --------------------------------------------------------
    # SHAP 計算
    # --------------------------------------------------------
    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X)

    # --------------------------------------------------------
    # 可視化
    # --------------------------------------------------------
    print("========================================")
    print(" TONOSAMA BUY SHAP ANALYSIS")
    print("----------------------------------------")
    print(" Features order:")
    for i, f in enumerate(FEATURES, 1):
        print(f"  {i}. {f}")
    print("========================================")

    # サマリープロット（重要度）
    shap.summary_plot(
        shap_values,
        X,
        plot_type="bar",
        show=False
    )
    plt.title("TONOSAMA BUY - Feature Importance (SHAP)")
    plt.tight_layout()
    plt.show()

    # 分布プロット（寄与の向き）
    shap.summary_plot(
        shap_values,
        X,
        show=False
    )
    plt.title("TONOSAMA BUY - SHAP Value Distribution")
    plt.tight_layout()
    plt.show()


# ============================================================
# エントリーポイント
# ============================================================

if __name__ == "__main__":
    main()