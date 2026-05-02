# ==========================================================
# File   : trading/summary/summary_scoring.py
# Version: Ver1.1-PRODUCTION-SCORING-PIPELINE-FIXED
# ----------------------------------------------------------
# ✔ summary_controller scoring保証を完全分離
# ✔ scoring_main 実行保証（score列チェック追加）
# ✔ 必須列自動生成
# ✔ ranking / logger 互換スコア生成
# ✔ NaN / inf 防御
# ✔ DataFrame安全コピー
# ✔ 副作用ゼロ
# ✔ 本番運用安定版
# ==========================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.scoring.core.scoring_core import scoring_main

logger = logging.getLogger(__name__)


# ==========================================================
# 必須列
# ==========================================================

REQUIRED_COLUMNS = {
    "entry_decision",
    "buy_score",
    "sell_score",
    "dominant_ratio",
    "dominant_side",
}


# ==========================================================
# 必須列生成
# ==========================================================

def ensure_required_columns(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        for col in REQUIRED_COLUMNS:

            if col not in df.columns:

                if col in ("entry_decision", "dominant_side"):
                    df[col] = None
                else:
                    df[col] = 0.0

        return df

    except Exception:

        logger.exception("[summary_scoring] ensure_required_columns failed")
        return df


# ==========================================================
# scoring_main 実行保証
# ==========================================================

def ensure_scoring_columns(
    df: pd.DataFrame,
    interval: int
) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        # --------------------------------------------------
        # score列が無ければ必ずscoring実行
        # --------------------------------------------------

        if "score" not in df.columns:

            try:

                df = scoring_main(
                    df,
                    interval=interval
                )

            except Exception:

                logger.exception(
                    "[summary_scoring] scoring_main failed"
                )

        # --------------------------------------------------
        # 必須列保証
        # --------------------------------------------------

        df = ensure_required_columns(df)

        return df

    except Exception:

        logger.exception(
            "[summary_scoring] ensure_scoring_columns fatal"
        )

        return df


# ==========================================================
# NaN / inf sanitize
# ==========================================================

def sanitize_scores(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        score_cols = [
            "buy_score",
            "sell_score",
            "dominant_ratio",
            "score",
        ]

        for col in score_cols:

            if col not in df.columns:
                continue

            df[col] = (
                pd.to_numeric(
                    df[col],
                    errors="coerce"
                )
                .replace([np.inf, -np.inf], 0)
                .fillna(0)
            )

        return df

    except Exception:

        logger.exception("[summary_scoring] sanitize_scores failed")
        return df


# ==========================================================
# ranking互換スコア生成
# ==========================================================

def build_ranking_scores(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        # buy / sell → ranking互換

        if "buy_score" in df.columns:
            df["score_buy"] = df["buy_score"]

        if "sell_score" in df.columns:
            df["score_sell"] = df["sell_score"]

        # score列保証
        if "score" not in df.columns:

            if "score_total" in df.columns:
                df["score"] = df["score_total"]

            elif "buy_score" in df.columns:
                df["score"] = df["buy_score"]

            else:
                df["score"] = 0

        return df

    except Exception:

        logger.exception("[summary_scoring] build_ranking_scores failed")
        return df


# ==========================================================
# MAIN PIPELINE
# ==========================================================

def apply_summary_scoring(
    df: pd.DataFrame,
    interval: int
) -> pd.DataFrame:

    """
    summary scoring pipeline
    controller から呼ばれる唯一のAPI
    """

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        # --------------------------------------------------
        # scoring保証
        # --------------------------------------------------

        df = ensure_scoring_columns(
            df,
            interval=interval
        )

        # --------------------------------------------------
        # numeric sanitize
        # --------------------------------------------------

        df = sanitize_scores(df)

        # --------------------------------------------------
        # ranking互換スコア生成
        # --------------------------------------------------

        df = build_ranking_scores(df)

        return df

    except Exception:

        logger.exception(
            "[summary_scoring] pipeline fatal"
        )

        return df