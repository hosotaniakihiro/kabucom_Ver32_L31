# ============================================================
# score_attach.py
# ------------------------------------------------------------
# ・signals で検出された条件を score_total に変換
# ・score_labels / score_reasons を一元管理
# ・BUY / SELL を分離しない（符号付き）
# ・★ score 加算内容をログで完全可視化 ★
# ============================================================

import logging
import pandas as pd
from scoring.utils.scorer_utils import normalize_reason

import logging

logger = logging.getLogger("score_trace")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
    )
    logger.addHandler(handler)

logger.propagate = False



def attach_scores(
    df: pd.DataFrame,
    *,
    score: int,
    labels: list[str],
):
    """
    Parameters
    ----------
    df : pd.DataFrame
        対象 DataFrame
    score : int
        加算する score（BUY:+ / SELL:-）
    labels : list[str]
        英語キーの理由ラベル
    """

    if df is None or df.empty or score == 0:
        return df

    df = df.copy()

    if "score_total" not in df.columns:
        df["score_total"] = 0

    if "score_labels" not in df.columns:
        df["score_labels"] = [[] for _ in range(len(df))]

    if "score_reasons" not in df.columns:
        df["score_reasons"] = [[] for _ in range(len(df))]

    for i in df.index:
        before = df.at[i, "score_total"]
        df.at[i, "score_total"] += score
        after = df.at[i, "score_total"]

        # ----------------------------------------------------
        # ★ スコア加算ログ（ここが可視化の核心）
        # ----------------------------------------------------
        logger.info(
            "[SCORE] idx=%s %+d | %+.1f → %+.1f | labels=%s",
            i,
            score,
            before,
            after,
            labels,
        )

        for lb in labels:
            if lb not in df.at[i, "score_labels"]:
                df.at[i, "score_labels"].append(lb)
                df.at[i, "score_reasons"].append(lb)

    return df


# ============================================================
# 絶対テクニカル条件（RSI / BB）
# ============================================================
def apply_absolute_technical_score(row, score, reasons, score_table):
    rsi = row.get("rsi")
    close = row.get("close_price")
    bb_l = row.get("bb_lower")
    bb_u = row.get("bb_upper")

    # --- RSI ---
    if rsi is not None:
        if rsi <= 30 and "rsi_oversold_30" in score_table:
            v = score_table["rsi_oversold_30"]
            before = score
            score += v
            reasons["RSI<=30"] = v

            logger.info(
                "[SCORE][ABS] RSI<=30 %+d | %+.1f → %+.1f",
                v,
                before,
                score,
            )

        if rsi >= 70 and "rsi_overbought_70" in score_table:
            v = score_table["rsi_overbought_70"]
            before = score
            score += v
            reasons["RSI>=70"] = v

            logger.info(
                "[SCORE][ABS] RSI>=70 %+d | %+.1f → %+.1f",
                v,
                before,
                score,
            )

    # --- Bollinger Band ---
    if close is not None:
        if bb_l is not None and close <= bb_l and "bb_lower_touch" in score_table:
            v = score_table["bb_lower_touch"]
            before = score
            score += v
            reasons["BB_LOWER_TOUCH"] = v

            logger.info(
                "[SCORE][ABS] BB_LOWER_TOUCH %+d | %+.1f → %+.1f",
                v,
                before,
                score,
            )

        if bb_u is not None and close >= bb_u and "bb_upper_touch" in score_table:
            v = score_table["bb_upper_touch"]
            before = score
            score += v
            reasons["BB_UPPER_TOUCH"] = v

            logger.info(
                "[SCORE][ABS] BB_UPPER_TOUCH %+d | %+.1f → %+.1f",
                v,
                before,
                score,
            )

    return score, reasons


# ============================================================
# 表示用整形（最後に1回だけ呼ぶ）
# ============================================================
def finalize_score_reasons(df: pd.DataFrame) -> pd.DataFrame:
    """
    ★ score_reasons は絶対に破壊しない ★
    表示・保存用の最終整形のみ
    """

    if df is None or df.empty:
        return df

    # score_reasons が無い場合のみ初期化
    if "score_reasons" not in df.columns:
        df["score_reasons"] = [{} for _ in range(len(df))]

    # None / NaN 防止（dict 保証）
    df["score_reasons"] = df["score_reasons"].apply(
        lambda x: dict(x) if isinstance(x, dict) else {}
    )

    return df
