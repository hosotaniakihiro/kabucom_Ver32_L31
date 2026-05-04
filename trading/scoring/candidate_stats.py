# ============================================================
# trading/scoring/candidate_stats.py
# Ver2.1-FINAL-CANDIDATE-STATS-SUMMARY-RANKING-COMPAT
# ------------------------------------------------------------
# ✔ SUMMARY / RANKING 共通ロガー
# ✔ source / interval 両対応（後方互換）
# ✔ df 空・None 完全耐性
# ✔ デバッグ・統計用途専用（売買ロジック非介入）
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Candidate Stats Logger
# ============================================================
def log_candidate_stats(
    df: pd.DataFrame,
    interval: int | None = None,
    source: str | None = None,
):
    """
    候補銘柄の統計ログを出力する（SUMMARY / RANKING 共通）

    Parameters
    ----------
    df : pd.DataFrame
        対象データ（ENTRY候補）
    interval : int | None
        1 / 3 / 5 などの分足（任意）
    source : str | None
        SUMMARY / RANKING / UNKNOWN
    """

    # --------------------------------------------------------
    # guard
    # --------------------------------------------------------
    if df is None or df.empty:
        logger.info("[CANDIDATE_STATS] empty df → skip")
        return

    src = source or "UNKNOWN"
    rows = len(df)

    # --------------------------------------------------------
    # 基本情報
    # --------------------------------------------------------
    if interval is not None:
        logger.info(
            "[CANDIDATE_STATS][%s][%dmin] rows=%d",
            src,
            interval,
            rows,
        )
    else:
        logger.info(
            "[CANDIDATE_STATS][%s] rows=%d",
            src,
            rows,
        )

    # --------------------------------------------------------
    # BUY / SELL 内訳
    # --------------------------------------------------------
    if "entry_decision" in df.columns:
        try:
            counts = df["entry_decision"].value_counts().to_dict()
            logger.info(
                "[CANDIDATE_STATS][%s] decision_counts=%s",
                src,
                counts,
            )
        except Exception:
            logger.exception("[CANDIDATE_STATS] decision count failed")

    # --------------------------------------------------------
    # スコア統計
    # --------------------------------------------------------
    for col in ("buy_score", "sell_score"):
        if col in df.columns:
            try:
                logger.info(
                    "[CANDIDATE_STATS][%s] %s min=%.3f max=%.3f mean=%.3f",
                    src,
                    col,
                    df[col].min(),
                    df[col].max(),
                    df[col].mean(),
                )
            except Exception:
                logger.exception(
                    "[CANDIDATE_STATS] score stat failed (%s)", col
                )

    # --------------------------------------------------------
    # dominant_ratio
    # --------------------------------------------------------
    if "dominant_ratio" in df.columns:
        try:
            logger.info(
                "[CANDIDATE_STATS][%s] dominant_ratio min=%.3f max=%.3f mean=%.3f",
                src,
                df["dominant_ratio"].min(),
                df["dominant_ratio"].max(),
                df["dominant_ratio"].mean(),
            )
        except Exception:
            logger.exception("[CANDIDATE_STATS] dominant_ratio stat failed")

    # --------------------------------------------------------
    # 参考：上位数行を DEBUG レベルで出す
    # --------------------------------------------------------
    try:
        preview_cols = [
            c for c in [
                "symbol",
                "symbolname",
                "entry_decision",
                "buy_score",
                "sell_score",
                "dominant_ratio",
            ]
            if c in df.columns
        ]

        if preview_cols:
            for _, r in df.head(5).iterrows():
                logger.debug(
                    "[CANDIDATE_STATS][%s] %s",
                    src,
                    {c: r.get(c) for c in preview_cols},
                )

    except Exception:
        logger.exception("[CANDIDATE_STATS] preview log failed")