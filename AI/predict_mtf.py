# ============================================================
# AI/predict_mtf.py
# ------------------------------------------------------------
# ✔ ENTRY 用 MTF AI（リアルタイム）
# ✔ クラスタ別 + 時間足別モデル対応（1M / 3M / 5M）
# ✔ summary / daily / ranking を feature source に統合
# ✔ feature 欠損時は安全に低信頼で返す
# ✔ NAS 配置・複数PC対応
# ✔ 無効化モデル（model_disabled.json）自動スキップ
# ✔ model_used / skip_reason を内部追跡
# ✔ positional / keyword 呼び出し完全互換
# ✔ inference crash safe
# ============================================================

from __future__ import annotations

import os
import json
import joblib
import numpy as np
import pandas as pd

from global_state import global_data


# ============================================================
# モデル格納
# ============================================================

BASE_DIR = "AI/models"

MODEL_FILES = {
    1: "model_1M.pkl",
    3: "model_3M.pkl",
    5: "model_5M.pkl",
}

DISABLED_MODEL_FILE = os.path.join(BASE_DIR, "model_disabled.json")

_MODEL_CACHE: dict[str, dict] = {}


# ============================================================
# disabled model
# ============================================================

def _is_model_disabled(path: str) -> bool:

    if not os.path.exists(DISABLED_MODEL_FILE):
        return False

    try:

        with open(DISABLED_MODEL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return path in data.get("disabled_models", [])

    except Exception:

        return False


# ============================================================
# model path resolve
# ============================================================

def _resolve_model_candidates(interval: int, cluster: int) -> list[str]:

    fname = MODEL_FILES.get(interval)

    if not fname:
        return []

    candidates: list[str] = []

    candidates.append(
        os.path.join(BASE_DIR, f"cluster{cluster}", fname)
    )

    candidates.append(
        os.path.join(BASE_DIR, fname)
    )

    return candidates


# ============================================================
# load model
# ============================================================

def _load_model(path: str) -> dict | None:

    if path in _MODEL_CACHE:
        return _MODEL_CACHE[path]

    try:

        data = joblib.load(path)

        _MODEL_CACHE[path] = data

        return data

    except Exception:

        return None


# ============================================================
# latest row
# ============================================================

def _latest_row(df, symbol: str) -> dict:

    if df is None or getattr(df, "empty", True):
        return {}

    if "symbol" not in df.columns:
        return {}

    d = df[df["symbol"] == symbol]

    if d.empty:
        return {}

    return d.iloc[-1].to_dict()


# ============================================================
# argument normalize
# ============================================================

def _normalize_args(*args, **kwargs):

    symbol = kwargs.get("symbol")
    price = kwargs.get("price")
    interval = kwargs.get("interval", 1)
    datetime = kwargs.get("datetime")

    if args:

        if len(args) >= 1:
            symbol = args[0]

        if len(args) >= 2:
            price = args[1]

        if len(args) >= 3:
            interval = args[2]

        if len(args) >= 4:
            datetime = args[3]

    return symbol, price, interval, datetime


# ============================================================
# MTF prediction
# ============================================================

def predict_mtf(*args, **kwargs) -> dict:
    """
    MTF-AI による方向確率を返す（ENTRY 用）

    positional / keyword 両対応
    """

    symbol, price, interval, datetime = _normalize_args(*args, **kwargs)

    if not symbol:

        return {
            "prob_up": 0.0,
            "prob_down": 0.0,
            "model_used": "NO_SYMBOL",
        }

    # =========================================================
    # cluster
    # =========================================================

    cluster_map = getattr(global_data, "symbol_cluster", {}) or {}
    cluster = cluster_map.get(symbol, 0)

    # =========================================================
    # feature row
    # =========================================================

    row: dict = {}

    row.update(_latest_row(getattr(global_data, "latest_summary_1m", None), symbol))
    row.update(_latest_row(getattr(global_data, "latest_summary_3m", None), symbol))
    row.update(_latest_row(getattr(global_data, "latest_summary_5m", None), symbol))

    row.update(_latest_row(getattr(global_data, "latest_daily_df", None), symbol))

    row.update(_latest_row(getattr(global_data, "latest_ranking_df", None), symbol))

    if not row:

        return {
            "prob_up": 0.0,
            "prob_down": 0.0,
            "model_used": "NO_FEATURE_ROW",
        }

    probs_up: list[float] = []
    probs_down: list[float] = []
    used_models: list[str] = []

    # =========================================================
    # inference
    # =========================================================

    for tf in (1, 3, 5):

        candidates = _resolve_model_candidates(tf, cluster)

        model_data = None
        model_path = None

        for path in candidates:

            if not os.path.exists(path):
                continue

            if _is_model_disabled(path):
                continue

            model_data = _load_model(path)

            if model_data:
                model_path = path
                break

        if not model_data:
            continue

        model = model_data.get("model")
        features = model_data.get("features")

        if model is None or not features:
            continue

        df = pd.DataFrame([row])

        for col in features:

            if col not in df.columns:
                df[col] = 0.0

        X = df[features].fillna(0)

        try:

            if hasattr(model, "predict_proba"):

                p = model.predict_proba(X)[0]

                probs_up.append(float(p[1]))
                probs_down.append(float(p[0]))

            else:

                pu = float(model.predict(X)[0])

                pu = max(0.0, min(1.0, pu))

                probs_up.append(pu)
                probs_down.append(1.0 - pu)

            used_models.append(os.path.basename(model_path))

        except Exception:
            continue

    if not probs_up:

        return {
            "prob_up": 0.0,
            "prob_down": 0.0,
            "model_used": "NO_MODEL_PREDICTED",
        }

    prob_up = float(np.mean(probs_up))
    prob_down = float(np.mean(probs_down))

    return {
        "prob_up": prob_up,
        "prob_down": prob_down,
        "model_used": ",".join(used_models) if used_models else "UNKNOWN",
    }