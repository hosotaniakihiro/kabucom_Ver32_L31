# ============================================================
# File: trading/ai/tonosama_detector.py
# Ver2.0-PRODUCTION-HARDENED-TONOSAMA-DETECTOR
# ------------------------------------------------------------
# ✔ ranking_velocity_1min 使用
# ✔ ranking_strength_1min 使用
# ✔ 出来高スパイク対応
# ✔ 価格加速対応
# ✔ NaN完全防御
# ✔ ENTRYフィルター用途
# ✔ dtype安全化
# ✔ DataFrame安全
# ✔ 副作用ゼロ
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# numeric safe
# ============================================================

def _safe_numeric(df: pd.DataFrame, col: str, default=0):

    try:

        if col not in df.columns:

            return pd.Series(default, index=df.index)

        s = pd.to_numeric(df[col], errors="coerce")

        s = s.replace([np.inf, -np.inf], np.nan)

        s = s.fillna(default)

        return s

    except Exception:

        logger.exception("[tonosama] numeric convert failed %s", col)

        return pd.Series(default, index=df.index)


# ============================================================
# DataFrame safety
# ============================================================

def _safe_df(df):

    try:

        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()

        df = df.copy()

        try:
            df = df.reset_index(drop=True)
        except Exception:
            pass

        return df

    except Exception:

        logger.exception("[tonosama] safe_df failed")

        return pd.DataFrame()


# ============================================================
# スコア計算
# ============================================================

def compute_tonosama_score(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df = _safe_df(df)

        if df.empty:
            return df

        df = df.copy()

        # ranking strength
        rank_count = _safe_numeric(df, "rank_count", 0)

        rank_best = _safe_numeric(df, "rank_best", 50)

        # ranking velocity
        velocity_score = _safe_numeric(df, "velocity_score", 0)

        # volume spike
        volume_ratio = _safe_numeric(df, "volume_ratio", 1)

        # price acceleration
        price_change = _safe_numeric(df, "price_change", 0)

        # ----------------------------------------------------
        # score
        # ----------------------------------------------------

        df["tonosama_score"] = (
            rank_count * 3
            + (50 - rank_best)
            + velocity_score * 2
            + volume_ratio * 4
            + price_change * 10
        )

        # numeric sanitize
        df["tonosama_score"] = (
            pd.to_numeric(df["tonosama_score"], errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

        return df

    except Exception:

        logger.exception("[tonosama] score compute failed")

        return df


# ============================================================
# 検出
# ============================================================

def detect_tonosama_candidates(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df = _safe_df(df)

        if df.empty:
            return pd.DataFrame()

        df = compute_tonosama_score(df)

        # safe numeric
        rank_best = _safe_numeric(df, "rank_best", 99)
        velocity = _safe_numeric(df, "velocity_score", 0)
        score = _safe_numeric(df, "tonosama_score", 0)

        cond = (
            (rank_best <= 10)
            & (velocity >= 8)
            & (score >= 60)
        )

        result = df.loc[cond].copy()

        if not result.empty:

            try:

                symbols = (
                    result["symbol"]
                    if "symbol" in result.columns
                    else result.index
                )

                logger.info(
                    "[TONOSAMA] detected %s symbols %s",
                    len(result),
                    list(symbols)[:10]
                )

            except Exception:

                logger.info(
                    "[TONOSAMA] detected %s symbols",
                    len(result)
                )

        return result.reset_index(drop=True)

    except Exception:

        logger.exception("[tonosama] detect failed")

        return pd.DataFrame()


# ============================================================
# ENTRYフィルター
# ============================================================

def allow_tonosama_entry(row: dict) -> bool:

    try:

        if not isinstance(row, dict):
            return False

        rank_best = row.get("rank_best", 99)

        velocity = row.get("velocity_score", 0)

        score = row.get("tonosama_score", 0)

        try:
            rank_best = float(rank_best)
        except Exception:
            rank_best = 99

        try:
            velocity = float(velocity)
        except Exception:
            velocity = 0

        try:
            score = float(score)
        except Exception:
            score = 0

        if rank_best <= 10 and velocity >= 8 and score >= 60:
            return True

        return False

    except Exception:

        logger.exception("[tonosama] entry filter failed")

        return False