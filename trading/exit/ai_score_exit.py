# ============================================================
# File   : trading/exit/ai_score_exit.py
# Version: Ver26.0-FINAL-AI-SCORE-EXIT-SAFE
# ------------------------------------------------------------
# ✔ EXIT AI スコア専用
# ✔ 決定権は持たない（補助のみ）
# ✔ モデル未ロード完全耐性
# ✔ NaN / object 完全排除
# ✔ 例外完全握り
# ✔ deterministic
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Dict, Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

_exit_score_model = None


# ============================================================
# 外部からモデル注入用
# ============================================================

def set_exit_score_model(model) -> None:
    global _exit_score_model
    _exit_score_model = model


# ============================================================
# 安全数値化
# ============================================================

def _safe_float(x: Any) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return v
    except Exception:
        return 0.0


# ============================================================
# 特徴量整形
# ============================================================

def _build_feature_df(features: Dict[str, Any]) -> pd.DataFrame:
    safe = {k: _safe_float(v) for k, v in features.items()}
    df = pd.DataFrame([safe])
    df = df.replace([np.inf, -np.inf], 0.0).fillna(0.0)
    return df


# ============================================================
# メインAIスコア
# ============================================================

def exit_decision_ai(
    symbol: str,
    side: str,
    pnl: float,
    features: Dict[str, Any] | None = None,
) -> Dict[str, float]:
    """
    EXIT AI スコア生成（補助用）

    return:
        {
            "exit_score": float,
            "exit_proba": float
        }

    例外時は安全値を返す。
    """

    try:
        # モデル無し → スコア0
        if _exit_score_model is None:
            return {
                "exit_score": 0.0,
                "exit_proba": 0.0,
            }

        if not features:
            return {
                "exit_score": 0.0,
                "exit_proba": 0.0,
            }

        df = _build_feature_df(features)

        # predict_proba 想定
        proba = _exit_score_model.predict_proba(df)[0][1]
        proba = _safe_float(proba)

        score = proba  # 今はそのまま使用（将来変換可）

        logger.debug(
            "[AI_EXIT_SCORE] symbol=%s side=%s pnl=%.2f score=%.4f",
            symbol,
            side,
            pnl,
            score,
        )

        return {
            "exit_score": score,
            "exit_proba": proba,
        }

    except Exception:
        logger.exception("[exit_decision_ai] exception")
        return {
            "exit_score": 0.0,
            "exit_proba": 0.0,
        }