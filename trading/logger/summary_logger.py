# ============================================================
# trading/logger/summary_logger.py
# Version: Ver1.2-PRODUCTION-SUMMARY-LOGGER-STABLE
# ------------------------------------------------------------
# ✔ SUMMARY ranking
# ✔ score列自動検出（score_total優先）
# ✔ symbol(symbolname) 表示
# ✔ RSI auto detect
# ✔ close / volume 列互換
# ✔ score整数表示
# ✔ price / volume 1桁表示
# ✔ NaN / inf 完全防御
# ✔ stable sort
# ✔ duplicate symbol remove
# ✔ 最新バーのみ表示
# ✔ list / dict / DataFrame 対応
# ✔ dtype stabilization
# ✔ ranking安定化
# ✔ logger crash protection
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from .format_utils import (
    safe_copy,
    safe_numeric,
    safe_symbolname,
    safe_close,
    safe_volume,
    safe_rsi,
    safe_float,
    fmt_score,
    fmt_float,
    latest_per_symbol,
)

logger = logging.getLogger(__name__)


# ============================================================
# SAFE NUMERIC SERIES
# ============================================================

def _safe_series(s):

    try:

        s = pd.to_numeric(s, errors="coerce")

        s = s.replace([np.inf, -np.inf], np.nan)

        s = s.fillna(0)

        return s.astype("float64")

    except Exception:

        return pd.Series(dtype="float64")


# ============================================================
# score列検出
# ============================================================

def _select_score_column(df: pd.DataFrame):

    try:

        # ---------------------------------------
        # 最優先
        # ---------------------------------------

        if "score_total" in df.columns:

            s = _safe_series(df["score_total"])

            if s.abs().sum() > 0:
                return "score_total"

        # ---------------------------------------
        # 互換
        # ---------------------------------------

        if "score" in df.columns:

            s = _safe_series(df["score"])

            if s.abs().sum() > 0:
                return "score"

        # ---------------------------------------
        # fallback
        # ---------------------------------------

        if "buy_score" in df.columns:

            s = _safe_series(df["buy_score"])

            if s.abs().sum() > 0:
                return "buy_score"

        # ---------------------------------------
        # night trading
        # ---------------------------------------

        if "night_weighted_score" in df.columns:

            s = _safe_series(df["night_weighted_score"])

            if s.abs().sum() > 0:
                return "night_weighted_score"

        return None

    except Exception:

        logger.exception("[SUMMARY LOGGER] score column detect failed")

        return None


# ============================================================
# SUMMARY RANKING
# ============================================================

def log_summary_ranking(
    df,
    interval: int | None = None,
    *,
    top_n: int = 10,
    min_score: float = 0.0,
    show_zero: bool = True,
):

    try:

        df = safe_copy(df)

        if df is None or df.empty:
            logger.info("[SUMMARY] データなし")
            return

        if "symbol" not in df.columns:
            logger.warning("[SUMMARY] symbol列が存在しません")
            return

        # ----------------------------------------------------
        # score column
        # ----------------------------------------------------

        score_col = _select_score_column(df)

        if score_col is None:
            logger.warning("[SUMMARY] score列が存在しません")
            return

        # ----------------------------------------------------
        # latest per symbol
        # ----------------------------------------------------

        df = latest_per_symbol(df)

        if df.empty:
            return

        # ----------------------------------------------------
        # numeric safety
        # ----------------------------------------------------

        df[score_col] = _safe_series(df[score_col])

        # ----------------------------------------------------
        # filter
        # ----------------------------------------------------

        if not show_zero:
            df = df[df[score_col] != 0]

        df = df[df[score_col] >= min_score]

        if df.empty:
            logger.info("[SUMMARY] 表示対象なし")
            return

        # ----------------------------------------------------
        # stable sort
        # ----------------------------------------------------

        df = df.sort_values(
            by=[score_col, "symbol"],
            ascending=[False, True],
            kind="mergesort",
        )

        rank = df.head(top_n)

        # ----------------------------------------------------
        # header
        # ----------------------------------------------------

        if interval is not None:

            logger.info(
                "========== 📊 SUMMARY RANKING (%smin) ==========",
                interval,
            )

        else:

            logger.info(
                "========== 📊 SUMMARY RANKING =========="
            )

        # ----------------------------------------------------
        # ranking
        # ----------------------------------------------------

        for i, r in enumerate(rank.itertuples(), 1):

            try:

                symbol = str(getattr(r, "symbol", "不明"))

                name = safe_symbolname(r)

                close = fmt_float(safe_close(r))

                volume = fmt_float(safe_volume(r))

                ma75 = fmt_float(
                    safe_float(getattr(r, "ma75", None))
                )

                rsi = fmt_float(safe_rsi(r))

                score = fmt_score(getattr(r, score_col, 0))

                logger.info(
                    "%2d. %s(%s) score=%3d C=%s V=%s MA75=%s RSI=%s",
                    i,
                    symbol,
                    name,
                    score,
                    close,
                    volume,
                    ma75,
                    rsi,
                )

            except Exception:

                logger.exception("[SUMMARY LOGGER ROW ERROR]")

    except Exception:

        logger.exception("[SUMMARY LOGGER ERROR]")


# ============================================================
# SUMMARY TOP SYMBOLS
# ============================================================

def get_top_symbols(
    df,
    *,
    top_n: int = 10,
):

    try:

        df = safe_copy(df)

        if df is None or df.empty:
            return []

        if "symbol" not in df.columns:
            return []

        score_col = _select_score_column(df)

        if score_col is None:
            return []

        df = latest_per_symbol(df)

        if df.empty:
            return []

        df[score_col] = _safe_series(df[score_col])

        df = df.sort_values(
            by=score_col,
            ascending=False,
            kind="mergesort",
        )

        rank = df.head(top_n)

        return rank["symbol"].astype(str).tolist()

    except Exception:

        logger.exception("[SUMMARY LOGGER ERROR]")

        return []