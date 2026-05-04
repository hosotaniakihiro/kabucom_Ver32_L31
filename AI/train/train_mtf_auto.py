# ============================================================
# AI/train/train_mtf_auto.py
# ------------------------------------------------------------
# ✔ MTF AI 自動学習（夜間バッチ）
# ✔ クラスタ別 × 時間足別（1M / 3M / 5M）
# ✔ NAS データ対応 / 複数PC実行可
# ✔ 重複学習防止（既存 model はスキップ）
# ✔ 失敗モデルは自動で無効化
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import os
import json
import joblib
import logging
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from lightgbm import LGBMClassifier

from config.paths import get_path

# ============================================================
# 設定
# ============================================================

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# paths.py 経由
# ------------------------------------------------------------
SUMMARY_DIR: Path = get_path("runtime_summary")
BASE_MODEL_DIR: Path = get_path("ai_models") / "mtf"
BASE_MODEL_DIR.mkdir(parents=True, exist_ok=True)

DISABLED_MODEL_FILE: Path = BASE_MODEL_DIR / "model_disabled.json"

TIMEFRAMES = [1, 3, 5]

# 最低学習条件
MIN_ROWS = 2000
TEST_SIZE = 0.2
RANDOM_STATE = 42


# ============================================================
# メイン
# ============================================================

def train_all_clusters():
    """
    全クラスタ × 全時間足を自動学習
    """
    clusters = _detect_clusters()

    for cluster in clusters:
        for tf in TIMEFRAMES:
            try:
                train_one(cluster, tf)
            except Exception:
                logger.exception("[TRAIN FAILED] cluster=%s tf=%s", cluster, tf)
                _disable_model(cluster, tf)


# ============================================================
# 個別学習
# ============================================================

def train_one(cluster: int, interval: int):
    """
    単一モデル学習
    """
    model_path = _model_path(cluster, interval)
    if model_path.exists():
        logger.info("[SKIP] exists %s", model_path)
        return

    logger.info("[TRAIN] cluster=%s interval=%sM", cluster, interval)

    df = _load_training_data(cluster, interval)
    if df is None or len(df) < MIN_ROWS:
        logger.warning(
            "[SKIP] insufficient data rows=%s",
            0 if df is None else len(df),
        )
        return

    X, y = _build_xy(df)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y,
        test_size=TEST_SIZE,
        random_state=RANDOM_STATE,
        stratify=y,
    )

    model = LGBMClassifier(
        n_estimators=300,
        learning_rate=0.05,
        max_depth=6,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=RANDOM_STATE,
    )

    model.fit(X_train, y_train)

    acc = model.score(X_test, y_test)
    logger.info(
        "[TRAIN DONE] cluster=%s tf=%s acc=%.3f rows=%d",
        cluster, interval, acc, len(df),
    )

    payload = {
        "model": model,
        "features": list(X.columns),
        "meta": {
            "cluster": cluster,
            "interval": interval,
            "rows": len(df),
            "accuracy": float(acc),
        },
    }

    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, model_path)


# ============================================================
# データ構築
# ============================================================

def _load_training_data(cluster: int, interval: int) -> pd.DataFrame | None:
    """
    NAS 上の summary DB から学習データを構築
    """
    tf_name = f"{interval}min"
    dfs: List[pd.DataFrame] = []

    for db in SUMMARY_DIR.glob("summary*.db"):
        try:
            df = pd.read_sql(
                f"SELECT * FROM stock_summary_{tf_name}",
                f"sqlite:///{db}",
            )
            df = df[df["cluster"] == cluster]
            dfs.append(df)
        except Exception:
            continue

    if not dfs:
        return None

    df = pd.concat(dfs, ignore_index=True)
    df = df.sort_values("datetime")

    # 未来リーク防止ラベル（次バー上昇）
    df["target"] = (
        df["close_price"].shift(-1) > df["close_price"]
    ).astype(int)

    df = df.dropna()
    return df


def _build_xy(df: pd.DataFrame):
    """
    学習用 X / y
    """
    drop_cols = {
        "id", "datetime", "symbol", "symbolname",
        "target",
    }

    feature_cols = [c for c in df.columns if c not in drop_cols]

    X = df[feature_cols]
    y = df["target"].astype(int)

    return X, y


# ============================================================
# クラスタ検出
# ============================================================

def _detect_clusters() -> List[int]:
    """
    summary DB から存在クラスタ検出
    """
    clusters = set()

    for db in SUMMARY_DIR.glob("summary*.db"):
        try:
            df = pd.read_sql(
                "SELECT DISTINCT cluster FROM stock_summary_1min",
                f"sqlite:///{db}",
            )
            clusters.update(
                df["cluster"].dropna().astype(int).tolist()
            )
        except Exception:
            continue

    return sorted(clusters)


# ============================================================
# モデル管理
# ============================================================

def _model_path(cluster: int, interval: int) -> Path:
    return BASE_MODEL_DIR / f"cluster{cluster}" / f"model_{interval}M.pkl"


def _disable_model(cluster: int, interval: int):
    """
    学習失敗モデルを無効化
    """
    path = str(_model_path(cluster, interval))

    disabled = set()
    if DISABLED_MODEL_FILE.exists():
        with open(DISABLED_MODEL_FILE, "r", encoding="utf-8") as f:
            disabled = set(json.load(f).get("disabled_models", []))

    disabled.add(path)

    with open(DISABLED_MODEL_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"disabled_models": sorted(disabled)},
            f,
            indent=2,
            ensure_ascii=False,
        )


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    train_all_clusters()
