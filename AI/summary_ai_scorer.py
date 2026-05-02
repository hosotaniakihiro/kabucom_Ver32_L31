# ============================================================
# summary_ai_scorer.py
# (Ver27-FINAL-SUMMARY-AI-SCORER)
# ------------------------------------------------------------
# ✔ SUMMARY に AI 評価を適用する実行レイヤ
# ✔ evaluator を呼ぶだけ（ロジック非保持）
# ✔ ENTRY / RANKING / SUMMARY すべてから安全に利用可
# ✔ calculator.py / analysis_logger 完全対応
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from AI.summary_ai_evaluator import evaluate_summary_ai

logger = logging.getLogger(__name__)


# ============================================================
# メイン API（SUMMARY 用 AI スコア付与）
# ============================================================
def score_summary_ai(
    summary_df: pd.DataFrame,
    *,
    enable_sell: bool = False,
) -> pd.DataFrame:
    """
    SUMMARY DataFrame に AI buy_score / sell_score を付与する

    Parameters
    ----------
    summary_df : DataFrame
        calculate_summary() の戻り値
    enable_sell : bool
        sell_score も付与する場合 True

    Returns
    -------
    DataFrame
        AI 評価済み summary_df（copy）
    """

    if summary_df is None or summary_df.empty:
        logger.debug("[SUMMARY_AI_SCORER] summary_df empty")
        return summary_df

    # --------------------------------------------------------
    # source 保険（SUMMARY 明示）
    # --------------------------------------------------------
    if "source" not in summary_df.columns:
        summary_df = summary_df.copy()
        summary_df["source"] = "SUMMARY"

    # --------------------------------------------------------
    # AI 評価適用（中身は evaluator に委譲）
    # --------------------------------------------------------
    try:
        scored_df = evaluate_summary_ai(
            summary_df,
            enable_sell=enable_sell,
        )
    except Exception as e:
        logger.exception(f"[SUMMARY_AI_SCORER] evaluate failed: {e}")
        return summary_df

    # --------------------------------------------------------
    # 最低限の後処理・検証
    # --------------------------------------------------------
    if "buy_score" not in scored_df.columns:
        logger.error("[SUMMARY_AI_SCORER] buy_score not found after evaluation")

    return scored_df


# ============================================================
# 互換エイリアス（将来の差し替え用）
# ============================================================
def apply_summary_ai(
    summary_df: pd.DataFrame,
    *,
    enable_sell: bool = False,
) -> pd.DataFrame:
    """
    score_summary_ai のエイリアス
    """
    return score_summary_ai(
        summary_df,
        enable_sell=enable_sell,
    )