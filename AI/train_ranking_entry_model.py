# ============================================================
# AI/train_ranking_entry_model.py
# Ver25.0-FINAL-RANKING-ENTRY-AI
# ------------------------------------------------------------
# ✔ ranking_feature_1min を入力とする ENTRY 判定モデル
# ✔ LightGBM 使用（高速・安定・特徴量相性◎）
# ✔ 学習 / 評価 / 保存まで単体完結
# ✔ 時系列リーク防止（shuffle=False）
# ✔ Ver25 系 feature 定義と完全一致
# ============================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, classification_report
import joblib

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

MODEL_DIR = Path("AI/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODEL_DIR / "ranking_entry_model.pkl"

# 使用する特徴量（ranking_feature_builder.py と完全一致）
FEATURE_COLUMNS: List[str] = [
    "appear_count",
    "best_rank",
    "avg_rank",
    "is_top10",
    "is_gain_rank",
    "is_volume_rank",
]

LABEL_COLUMN = "label"   # ← 事前に作成しておくこと


# ============================================================
# 学習データ前処理
# ============================================================

def _validate_training_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    学習前チェックと最小正規化
    """
    missing = [c for c in FEATURE_COLUMNS + [LABEL_COLUMN] if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    # 型固定（AI事故防止）
    df = df.copy()

    df["appear_count"] = df["appear_count"].astype(int)
    df["best_rank"] = df["best_rank"].astype(int)
    df["avg_rank"] = df["avg_rank"].astype(float)
    df["is_top10"] = df["is_top10"].astype(int)
    df["is_gain_rank"] = df["is_gain_rank"].astype(int)
    df["is_volume_rank"] = df["is_volume_rank"].astype(int)
    df[LABEL_COLUMN] = df[LABEL_COLUMN].astype(int)

    return df


# ============================================================
# MAIN: 学習
# ============================================================

def train_ranking_entry_model(
    df: pd.DataFrame,
    *,
    test_size: float = 0.2,
    random_state: int = 42,
) -> lgb.LGBMClassifier:
    """
    ランキング ENTRY AI を学習する

    Parameters
    ----------
    df : pd.DataFrame
        ranking_feature_1min + label
    test_size : float
        テストデータ割合（時系列分割）
    random_state : int
        再現性用（shuffle=False なので影響は限定的）

    Returns
    -------
    model : LGBMClassifier
    """

    logger.info("🧠 start training ranking entry model")

    df = _validate_training_df(df)

    X = df[FEATURE_COLUMNS]
    y = df[LABEL_COLUMN]

    # --------------------------------------------------------
    # 時系列分割（未来情報リーク防止）
    # --------------------------------------------------------
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=test_size,
        shuffle=False,
        random_state=random_state,
    )

    # --------------------------------------------------------
    # モデル定義
    # --------------------------------------------------------
    model = lgb.LGBMClassifier(
        objective="binary",
        boosting_type="gbdt",
        n_estimators=400,
        learning_rate=0.03,
        max_depth=5,
        num_leaves=32,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=random_state,
        n_jobs=-1,
    )

    # --------------------------------------------------------
    # 学習
    # --------------------------------------------------------
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_test, y_test)],
        eval_metric="auc",
        verbose=False,
    )

    # --------------------------------------------------------
    # 評価
    # --------------------------------------------------------
    pred_proba = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, pred_proba)

    logger.info("✅ Ranking ENTRY model AUC = %.4f", auc)

    # 分類レポート（任意・ログ用）
    pred_label = (pred_proba >= 0.5).astype(int)
    report = classification_report(y_test, pred_label, zero_division=0)
    logger.info("\n%s", report)

    # --------------------------------------------------------
    # 保存
    # --------------------------------------------------------
    joblib.dump(
        {
            "model": model,
            "features": FEATURE_COLUMNS,
            "auc": auc,
        },
        MODEL_PATH,
    )

    logger.info("💾 model saved: %s", MODEL_PATH)

    return model


# ============================================================
# CLI 実行用（任意）
# ============================================================

if __name__ == "__main__":
    """
    例:
    python AI/train_ranking_entry_model.py ranking_train.csv
    """

    import sys

    if len(sys.argv) < 2:
        print("usage: python train_ranking_entry_model.py <csv_path>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])

    if not csv_path.exists():
        print(f"file not found: {csv_path}")
        sys.exit(1)

    df_train = pd.read_csv(csv_path)

    train_ranking_entry_model(df_train)