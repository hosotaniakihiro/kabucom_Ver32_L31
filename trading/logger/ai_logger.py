# ============================================================
# trading/logger/ai_logger.py
# Ver1.1-PRODUCTION-AI-RANKING-LOGGER
# ------------------------------------------------------------
# ✔ AI ranking
# ✔ ai_score / ai_meta_score / alpha_score / model_score 対応
# ✔ symbol(symbolname)
# ✔ close / volume / RSI 表示
# ✔ NaN / inf 完全防御
# ✔ stable sort
# ✔ duplicate symbol remove
# ✔ 最新バーのみ表示
# ✔ list / dict / DataFrame 対応
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .format_utils import (
    safe_copy,
    safe_numeric,
    safe_symbolname,
    safe_close,
    safe_volume,
    safe_rsi,
    fmt_float,
    latest_per_symbol,
)

logger = logging.getLogger(__name__)


# ============================================================
# AI SCORE COLUMN DETECT
# ============================================================

def _detect_ai_score_column(df):

    for c in (
        "ai_score",
        "ai_meta_score",
        "alpha_score",
        "model_score",
    ):

        if c in df.columns:
            return c

    return None


# ============================================================
# AI RANKING
# ============================================================

def log_ai_meta_ranking(df, top_n: int = 10):

    try:

        df = safe_copy(df)

        if df.empty:
            return

        if "symbol" not in df.columns:
            return

        score_col = _detect_ai_score_column(df)

        if score_col is None:
            return

        # 最新バー
        df = latest_per_symbol(df)

        df[score_col] = safe_numeric(df[score_col])

        rank = (
            df.sort_values(score_col, ascending=False)
            .head(top_n)
        )

        if rank.empty:
            return

        logger.info("========== 🤖 AI META RANKING ==========")

        for i, r in enumerate(rank.itertuples(), 1):

            symbol = str(getattr(r, "symbol", "不明"))
            name = safe_symbolname(r)

            score = float(getattr(r, score_col, 0))

            close = fmt_float(safe_close(r))
            volume = fmt_float(safe_volume(r))
            rsi = fmt_float(safe_rsi(r))

            logger.info(
                "%2d. %s(%s) ai_score=%.2f C=%s V=%s RSI=%s",
                i,
                symbol,
                name,
                score,
                close,
                volume,
                rsi,
            )

    except Exception:

        logger.exception("[AI LOGGER ERROR]")


# ============================================================
# AI SIGNAL COUNT
# ============================================================

def count_ai_signals(df, threshold: float = 0.7):

    try:

        df = safe_copy(df)

        if df.empty:
            return 0

        score_col = _detect_ai_score_column(df)

        if score_col is None:
            return 0

        df[score_col] = safe_numeric(df[score_col])

        return int((df[score_col] >= threshold).sum())

    except Exception:

        logger.exception("[AI COUNT ERROR]")

        return 0


# ============================================================
# AI SYMBOL LIST
# ============================================================

def get_ai_symbols(df, threshold: float = 0.7):

    try:

        df = safe_copy(df)

        if df.empty:
            return []

        if "symbol" not in df.columns:
            return []

        score_col = _detect_ai_score_column(df)

        if score_col is None:
            return []

        df[score_col] = safe_numeric(df[score_col])

        df = df[df[score_col] >= threshold]

        if df.empty:
            return []

        return df["symbol"].astype(str).tolist()

    except Exception:

        logger.exception("[AI SYMBOL ERROR]")

        return []