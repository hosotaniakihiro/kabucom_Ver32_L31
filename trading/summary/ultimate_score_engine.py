# ============================================================
# trading/summary/ultimate_score_engine.py
# FINAL-ULTIMATE-SCORE-SYSTEM
# ------------------------------------------------------------
# ✔ rule score
# ✔ regime adapt
# ✔ bandit weight
# ✔ AI integration
# ✔ total unify
# ============================================================

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# ① RULE SCORE（flag集計）
# ------------------------------------------------------------
def apply_rule_score(df: pd.DataFrame) -> pd.DataFrame:

    rule_cols = [
        c for c in df.columns
        if c.startswith("buy_flag_") or c.startswith("sell_flag_")
    ]

    if rule_cols:
        df["score_rule"] = (
            df[rule_cols]
            .apply(pd.to_numeric, errors="coerce")
            .fillna(0)
            .sum(axis=1)
        )
    else:
        df["score_rule"] = 0.0

    return df


# ------------------------------------------------------------
# ② REGIME ADAPTATION
# ------------------------------------------------------------
def apply_regime_score(df: pd.DataFrame, market_state: dict | None):

    if market_state is None:
        df["score_regime"] = df["score_rule"]
        return df

    regime = market_state.get("regime", "neutral")

    if regime == "bull":
        factor = 1.2
    elif regime == "bear":
        factor = 0.8
    else:
        factor = 1.0

    df["score_regime"] = df["score_rule"] * factor
    return df


# ------------------------------------------------------------
# ③ BANDIT WEIGHTING
# ------------------------------------------------------------
def apply_bandit_score(df: pd.DataFrame, bandit_weights: dict | None):

    if bandit_weights is None:
        df["score_bandit"] = df["score_regime"]
        return df

    weight = bandit_weights.get("default", 1.0)

    df["score_bandit"] = df["score_regime"] * weight
    return df


# ------------------------------------------------------------
# ④ AI MODEL SCORE
# ------------------------------------------------------------
def apply_ai_score(df: pd.DataFrame, model=None):

    if model is None:
        df["score_ai"] = 0.0
        return df

    try:
        features = df.select_dtypes(include=[np.number]).fillna(0)
        preds = model.predict(features)
        df["score_ai"] = preds
    except Exception:
        logger.exception("AI score failed")
        df["score_ai"] = 0.0

    return df


# ------------------------------------------------------------
# ⑤ FINAL INTEGRATION
# ------------------------------------------------------------
def integrate_final_score(df: pd.DataFrame) -> pd.DataFrame:

    df["score_total"] = (
        df["score_bandit"].fillna(0)
        + df["score_ai"].fillna(0)
    )

    df["score"] = df["score_total"]

    return df


# ------------------------------------------------------------
# MASTER PIPELINE
# ------------------------------------------------------------
def apply_ultimate_score(
    df: pd.DataFrame,
    *,
    market_state=None,
    bandit_weights=None,
    ai_model=None,
):

    df = apply_rule_score(df)
    df = apply_regime_score(df, market_state)
    df = apply_bandit_score(df, bandit_weights)
    df = apply_ai_score(df, ai_model)
    df = integrate_final_score(df)

    return df