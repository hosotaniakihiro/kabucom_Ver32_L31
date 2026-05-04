# ============================================================
# AI/train/entry_probability_ml.py
# ------------------------------------------------------------
# ✔ ENTRY 勝率推定 ML モデル
# ✔ ヒューリスティックを置き換える「確率」専用
# ✔ LightGBM 想定（sklearn API）
# ✔ NAS / 夜間学習前提
# ============================================================

import os
import joblib
import numpy as np
import pandas as pd
from typing import Dict, Any

MODEL_PATH = "AI/models/entry_probability.pkl"


# ============================================================
# 推論 API（entry_probability.py から呼ばれる）
# ============================================================

def predict_probability_ml(row: Dict[str, Any]) -> float:
    """
    ENTRY 勝率を ML で推定する

    Returns:
        float: 0.0 - 1.0
    """

    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"model not found: {MODEL_PATH}")

    data = joblib.load(MODEL_PATH)
    model = data["model"]
    features = data["features"]

    df = _build_feature_df(row, features)
    prob = model.predict_proba(df)[0][1]

    return float(np.clip(prob, 0.0, 1.0))


# ============================================================
# 学習用 API（夜間ジョブ専用）
# ============================================================

def train_entry_probability_model(
    df: pd.DataFrame,
    *,
    target_col: str = "label_win",
    save_path: str = MODEL_PATH,
):
    """
    ENTRY 勝率モデルを学習する

    df:
        学習用 DataFrame
        - features
        - label_win (1: 勝ち, 0: 負け)

    保存形式:
        {
            "model": model,
            "features": feature_names,
        }
    """

    from lightgbm import LGBMClassifier

    feature_cols = _select_features(df)
    X = df[feature_cols].fillna(0)
    y = df[target_col].astype(int)

    model = LGBMClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
    )

    model.fit(X, y)

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "features": feature_cols,
        },
        save_path,
    )

    return {
        "rows": len(df),
        "features": len(feature_cols),
        "path": save_path,
    }


# ============================================================
# 内部ユーティリティ
# ============================================================

def _build_feature_df(row: Dict[str, Any], features: list) -> pd.DataFrame:
    """
    row(dict) → model 入力 DataFrame
    """

    data = {}
    for f in features:
        v = row.get(f)
        if v is None:
            data[f] = 0.0
        else:
            try:
                data[f] = float(v)
            except Exception:
                data[f] = 0.0

    return pd.DataFrame([data])


def _select_features(df: pd.DataFrame) -> list:
    """
    学習に使う特徴量を自動抽出
    """

    EXCLUDE = {
        "symbol",
        "datetime",
        "date",
        "entry_decision",
        "label_win",
        "pnl",
        "reason",
    }

    feature_cols = [
        c for c in df.columns
        if c not in EXCLUDE
        and df[c].dtype != "object"
    ]

    return feature_cols
