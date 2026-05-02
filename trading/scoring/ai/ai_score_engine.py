# ============================================================
# File   : trading/scoring/ai/ai_score_engine.py
# Version: Ver2.1-PRODUCTION-AI-SCORE-ENGINE-STABLE
# ------------------------------------------------------------
# ✔ AI score integration
# ✔ ranking momentum
# ✔ theme momentum
# ✔ liquidity filter
# ✔ volatility filter
# ✔ confidence score
# ✔ symbol別 momentum 計算
# ✔ vectorized
# ✔ NaN / inf safe
# ✔ missing column safe
# ✔ production stable
# ✔ backward compatible
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe_numeric(df, col, default=0):

    if col not in df.columns:
        return pd.Series(default, index=df.index)

    s = df[col]

    # DataFrame列混入防御
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]

    # tuple / list防御
    if isinstance(s, (tuple, list)):
        s = pd.Series(s, index=df.index)

    s = pd.to_numeric(s, errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    return s.fillna(default)

# ============================================================
# symbol aware diff
# ============================================================

def _symbol_diff(df: pd.DataFrame, series: pd.Series):

    if "symbol" not in df.columns:
        return series.diff()

    try:
        return series.groupby(df["symbol"]).diff()
    except Exception:
        return series.diff()


# ============================================================
# ranking momentum score
# ============================================================

def _ranking_momentum_score(df: pd.DataFrame):

    ranking = _safe_numeric(df, "ranking_score")

    velocity = _symbol_diff(df, ranking)

    momentum = (-velocity).clip(lower=0)

    return momentum * 1.5


# ============================================================
# theme momentum
# ============================================================

def _theme_momentum_score(df: pd.DataFrame):

    theme_strength = _safe_numeric(df, "theme_score")

    theme_change = _symbol_diff(df, theme_strength)

    return theme_change.clip(lower=0) * 1.2


# ============================================================
# liquidity score
# ============================================================

def _liquidity_score(df: pd.DataFrame):

    turnover = _safe_numeric(df, "turnover")

    liquidity = np.log1p(turnover)

    liquidity = liquidity.replace([np.inf, -np.inf], np.nan)

    return liquidity.fillna(0) * 0.5


# ============================================================
# volatility score
# ============================================================

def _volatility_score(df: pd.DataFrame):

    high = _safe_numeric(df, "high")
    low = _safe_numeric(df, "low")
    close = _safe_numeric(df, "close")

    volatility = (high - low) / (close + 1e-9)

    volatility = volatility.replace([np.inf, -np.inf], np.nan)

    return volatility.fillna(0) * 0.8


# ============================================================
# confidence score
# ============================================================

def _confidence_score(df: pd.DataFrame):

    ai_conf = _safe_numeric(df, "ai_confidence")
    prob = _safe_numeric(df, "ai_probability")

    conf = (ai_conf + prob)

    conf = conf.replace([np.inf, -np.inf], np.nan)

    return conf.fillna(0) * 1.5


# ============================================================
# sanitize result
# ============================================================

def _sanitize(series: pd.Series):

    return (
        series
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


# ============================================================
# apply AI scores
# ============================================================

def apply_ai_scores(
    df: pd.DataFrame,
    interval: str | None = None,
    force: bool = False,
    analysis_only: bool = False
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        df_out = df.copy()

        # ----------------------------------------------------
        # base AI score
        # ----------------------------------------------------

        base_ai = _safe_numeric(df_out, "ai_score")

        # ----------------------------------------------------
        # ranking momentum
        # ----------------------------------------------------

        ranking_momentum = _ranking_momentum_score(df_out)

        # ----------------------------------------------------
        # theme momentum
        # ----------------------------------------------------

        theme_momentum = _theme_momentum_score(df_out)

        # ----------------------------------------------------
        # liquidity
        # ----------------------------------------------------

        liquidity = _liquidity_score(df_out)

        # ----------------------------------------------------
        # volatility
        # ----------------------------------------------------

        volatility = _volatility_score(df_out)

        # ----------------------------------------------------
        # confidence
        # ----------------------------------------------------

        confidence = _confidence_score(df_out)

        # ----------------------------------------------------
        # combine AI score
        # ----------------------------------------------------

        final_ai_score = (
            base_ai * 2.0
            + ranking_momentum
            + theme_momentum
            + liquidity
            + volatility
            + confidence
        )

        final_ai_score = _sanitize(final_ai_score)

        df_out["ai_score"] = final_ai_score

        # optional debug
        if logger.isEnabledFor(logging.DEBUG):

            logger.debug(
                "[AI SCORE] rows=%s mean=%.3f max=%.3f",
                len(df_out),
                float(final_ai_score.mean()),
                float(final_ai_score.max()),
            )

        return df_out

    except Exception:

        logger.exception("[AI SCORE] error")

        return df


# ============================================================
# backward compatibility
# ============================================================

# scoring_core が apply_ai_score を呼ぶ場合の互換
def apply_ai_score(df: pd.DataFrame, *args, **kwargs):

    return apply_ai_scores(df, *args, **kwargs)