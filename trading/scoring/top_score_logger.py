

# ============================================================
# trading/scoring/top_score_logger.py
# Ver2.1-FINAL-TOP-SCORE-LOGGER-SUMMARY-RANKING-COMPAT
# ------------------------------------------------------------
# ✔ SUMMARY / RANKING 共通
# ✔ source / interval 両対応（後方互換）
# ✔ 上位スコア銘柄の可視化
# ✔ デバッグ・分析専用（売買ロジック非介入）
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Top Score Logger
# ============================================================
def log_top_scores(
    df: pd.DataFrame,
    interval: int | None = None,
    source: str | None = None,
    top_n: int = 5,
):
    """
    上位スコア銘柄をログ出力する

    Parameters
    ----------
    df : pd.DataFrame
        ENTRY候補 DataFrame
    interval : int | None
        1 / 3 / 5 分足など（任意）
    source : str | None
        SUMMARY / RANKING / UNKNOWN
    top_n : int
        上位何件表示するか
    """

    # --------------------------------------------------------
    # guard
    # --------------------------------------------------------
    if df is None or df.empty:
        logger.info("[TOP_SCORES] empty df → skip")
        return

    src = source or "UNKNOWN"

    # --------------------------------------------------------
    # score 列決定
    # --------------------------------------------------------
    score_col = None
    if "buy_score" in df.columns:
        score_col = "buy_score"
    elif "score" in df.columns:
        score_col = "score"

    if score_col is None:
        logger.warning("[TOP_SCORES][%s] score column missing", src)
        return

    # --------------------------------------------------------
    # 上位抽出
    # --------------------------------------------------------
    try:
        df_top = (
            df.sort_values(score_col, ascending=False)
            .head(top_n)
            .copy()
        )
    except Exception:
        logger.exception("[TOP_SCORES] sort failed")
        return

    # --------------------------------------------------------
    # ヘッダ
    # --------------------------------------------------------
    if interval is not None:
        logger.info(
            "[TOP_SCORES][%s][%dmin] top=%d by %s",
            src,
            interval,
            top_n,
            score_col,
        )
    else:
        logger.info(
            "[TOP_SCORES][%s] top=%d by %s",
            src,
            top_n,
            score_col,
        )

    # --------------------------------------------------------
    # 各行ログ
    # --------------------------------------------------------
    for _, r in df_top.iterrows():
        try:
            logger.info(
                "[TOP_SCORES][%s] %s (%s) score=%.3f dom=%.3f side=%s",
                src,
                r.get("symbolname"),
                r.get("symbol"),
                float(r.get(score_col, 0.0)),
                float(r.get("dominant_ratio", 0.0)),
                r.get("entry_decision"),
            )
        except Exception:
            logger.exception("[TOP_SCORES] row log failed")