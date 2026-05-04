# ============================================================
# trading/scoring/candidate_reason_stats.py
# Ver2.1-FINAL-CANDIDATE-REASON-STATS-SUMMARY-RANKING-COMPAT
# ------------------------------------------------------------
# ✔ SUMMARY / RANKING 共通
# ✔ source / interval 両対応（後方互換）
# ✔ buy / sell reason 可視化
# ✔ デバッグ・分析専用（売買ロジック非介入）
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Candidate Reason Stats Logger
# ============================================================
def log_candidate_reason_stats(
    df: pd.DataFrame,
    interval: int | None = None,
    source: str | None = None,
):
    """
    ENTRY候補に対する理由（reason）統計ログ

    Parameters
    ----------
    df : pd.DataFrame
        ENTRY候補 DataFrame
    interval : int | None
        1 / 3 / 5 分足など（任意）
    source : str | None
        SUMMARY / RANKING / UNKNOWN
    """

    # --------------------------------------------------------
    # guard
    # --------------------------------------------------------
    if df is None or df.empty:
        logger.info("[CANDIDATE_REASON] empty df → skip")
        return

    src = source or "UNKNOWN"

    # --------------------------------------------------------
    # 基本ログ
    # --------------------------------------------------------
    if interval is not None:
        logger.info(
            "[CANDIDATE_REASON][%s][%dmin] rows=%d",
            src,
            interval,
            len(df),
        )
    else:
        logger.info(
            "[CANDIDATE_REASON][%s] rows=%d",
            src,
            len(df),
        )

    # --------------------------------------------------------
    # buy / sell reasons 集計
    # --------------------------------------------------------
    for side in ("buy", "sell"):
        col = f"{side}_reasons"
        if col not in df.columns:
            continue

        try:
            reasons = (
                df[col]
                .dropna()
                .astype(str)
                .str.split(",")
                .explode()
                .str.strip()
            )

            if reasons.empty:
                continue

            counts = reasons.value_counts().to_dict()

            logger.info(
                "[CANDIDATE_REASON][%s] %s_reason_counts=%s",
                src,
                side.upper(),
                counts,
            )

        except Exception:
            logger.exception(
                "[CANDIDATE_REASON] %s reason aggregation failed", side
            )

    # --------------------------------------------------------
    # reason_scores（dict）統計
    # --------------------------------------------------------
    for side in ("buy", "sell"):
        col = f"{side}_reason_scores"
        if col not in df.columns:
            continue

        try:
            merged: dict[str, float] = {}

            for d in df[col]:
                if not isinstance(d, dict):
                    continue
                for k, v in d.items():
                    merged[k] = merged.get(k, 0.0) + float(v)

            if merged:
                logger.info(
                    "[CANDIDATE_REASON][%s] %s_reason_scores=%s",
                    src,
                    side.upper(),
                    dict(sorted(merged.items(), key=lambda x: -x[1])),
                )

        except Exception:
            logger.exception(
                "[CANDIDATE_REASON] %s reason_scores failed", side
            )