# ============================================================
# AI/ranking_entry_predictor.py
# Ver25.0-FINAL-RANKING-ENTRY-PREDICTOR
# ------------------------------------------------------------
# ✔ ranking_feature_1min を入力として ENTRY 可否を判定
# ✔ train_ranking_entry_model.py と完全整合
# ✔ モデル / 特徴量の不一致を構造的に防止
# ✔ スコア・判定を同時に返却（実運用向け）
# ✔ 軽量・同期処理（ENTRY パス上で安全）
# ============================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Any

import pandas as pd
import joblib

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

MODEL_PATH = Path("AI/models/ranking_entry_model.pkl")

# ENTRY 判定の閾値（運用で調整）
DEFAULT_ENTRY_THRESHOLD = 0.65


# ============================================================
# モデルロード（1回だけ）
# ============================================================

_model = None
_feature_columns = None
_model_auc = None


def _load_model():
    """
    モデルを遅延ロード（初回のみ）
    """
    global _model, _feature_columns, _model_auc

    if _model is not None:
        return

    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"ranking entry model not found: {MODEL_PATH}"
        )

    data = joblib.load(MODEL_PATH)

    _model = data["model"]
    _feature_columns = data["features"]
    _model_auc = data.get("auc")

    logger.info(
        "🧠 ranking entry model loaded (AUC=%.4f)",
        _model_auc if _model_auc is not None else -1,
    )


# ============================================================
# 内部ユーティリティ
# ============================================================

def _validate_feature_row(feature_row: Dict[str, Any]) -> pd.DataFrame:
    """
    feature_row を DataFrame（1行）に変換し、型と列を保証
    """

    _load_model()

    # 欠損チェック
    missing = [c for c in _feature_columns if c not in feature_row]
    if missing:
        raise ValueError(f"missing feature columns: {missing}")

    # DataFrame 化
    df = pd.DataFrame([feature_row])

    # 型固定（AI事故防止）
    for col in _feature_columns:
        if col.startswith("is_") or col in ("appear_count", "best_rank"):
            df[col] = df[col].astype(int)
        else:
            df[col] = df[col].astype(float)

    return df[_feature_columns]


# ============================================================
# MAIN: ENTRY 判定
# ============================================================

def predict_ranking_entry(
    feature_row: Dict[str, Any],
    *,
    threshold: float = DEFAULT_ENTRY_THRESHOLD,
) -> Dict[str, Any]:
    """
    ランキング特徴量から ENTRY 可否を判定する

    Parameters
    ----------
    feature_row : dict
        ranking_feature_builder.py が生成した 1行分
    threshold : float
        ENTRY 判定閾値

    Returns
    -------
    dict:
        {
            "allow": bool,
            "score": float,
            "threshold": float,
            "reason": str
        }
    """

    df = _validate_feature_row(feature_row)

    # --------------------------------------------------------
    # 推論
    # --------------------------------------------------------
    proba = _model.predict_proba(df)[0][1]
    allow = proba >= threshold

    # --------------------------------------------------------
    # 理由（ログ・デバッグ用）
    # --------------------------------------------------------
    if allow:
        reason = f"score={proba:.3f} >= threshold={threshold:.2f}"
    else:
        reason = f"score={proba:.3f} < threshold={threshold:.2f}"

    return {
        "allow": bool(allow),
        "score": float(proba),
        "threshold": float(threshold),
        "reason": reason,
    }


# ============================================================
# UTIL: スコアのみ取得（軽量）
# ============================================================

def predict_ranking_score(feature_row: Dict[str, Any]) -> float:
    """
    ENTRY スコアのみ返す（高速・ログ不要な場合）
    """
    df = _validate_feature_row(feature_row)
    return float(_model.predict_proba(df)[0][1])