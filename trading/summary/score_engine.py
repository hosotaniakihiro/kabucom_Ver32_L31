# ============================================================
# trading/summary/score_engine.py
# FINAL-UNIFIED-SCORE-ENGINE
# ------------------------------------------------------------
# ✔ score_* 自動検出
# ✔ score_total 自動生成
# ✔ score 別名保証
# ✔ 数値型強制
# ✔ 全ルート共通利用
# ============================================================

import pandas as pd
import logging

logger = logging.getLogger(__name__)


def apply_score(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # ----------------------------------------
    # score_* 自動抽出
    # ----------------------------------------
    score_cols = [
        c for c in df.columns
        if c.startswith("score_") and c != "score_total"
    ]

    numeric_cols = []

    for c in score_cols:
        if pd.api.types.is_numeric_dtype(df[c]):
            numeric_cols.append(c)
        else:
            logger.warning(
                "[score_engine] non-numeric score skipped: %s",
                c,
            )

    if numeric_cols:
        df["score_total"] = (
            df[numeric_cols]
            .apply(pd.to_numeric, errors="coerce")
            .fillna(0)
            .sum(axis=1)
        )
    else:
        df["score_total"] = 0.0

    # print互換
    df["score"] = df["score_total"]

    # 型固定
    df["score_total"] = pd.to_numeric(
        df["score_total"], errors="coerce"
    ).fillna(0.0)

    df["score"] = pd.to_numeric(
        df["score"], errors="coerce"
    ).fillna(0.0)

    return df