# ============================================================
# File   : monitoring/summary_monitor.py
# Version: Ver1.0-PRODUCTION-SUMMARY-MONITOR
# ------------------------------------------------------------
# ✔ PUSH OHLC summary 表示
# ✔ ranking snapshot summary 表示
# ✔ 定時ログ
# ✔ DataFrame安全
# ✔ crash safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import datetime as dt

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# utility
# ============================================================

def _safe_df(df):

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    return df


# ============================================================
# PUSH OHLC summary
# ============================================================

def _print_push_summary():

    df = _safe_df(getattr(global_data, "summary_1min", None))

    if df.empty:
        logger.info("[MONITOR] summary_1min empty")
        return

    if "datetime" in df.columns:
        latest_time = df["datetime"].max()
    else:
        latest_time = None

    logger.info("")
    logger.info("=== ⏱ PUSH 1min SUMMARY | %s ===", latest_time)

    latest = df.sort_values("datetime").groupby("symbol").tail(1)

    latest = latest.sort_values(
        "score_buy" if "score_buy" in latest.columns else "close",
        ascending=False
    )

    for _, r in latest.head(10).iterrows():

        symbol = r.get("symbol", "?")
        close = r.get("close", 0)
        slope = r.get("slope", 0)
        score = r.get("score_buy", 0)

        logger.info(
            "%s  close=%s  slope=%.4f  score=%s",
            symbol,
            close,
            slope,
            score
        )


# ============================================================
# ranking snapshot summary
# ============================================================

def _print_ranking_summary():

    df = _safe_df(getattr(global_data, "ranking_snapshot", None))

    if df.empty:
        logger.info("[MONITOR] ranking_snapshot empty")
        return

    logger.info("")
    logger.info("========== 📊 RANKING SNAPSHOT ==========")

    df = df.sort_values(
        "score_buy" if "score_buy" in df.columns else df.columns[0],
        ascending=False
    )

    for _, r in df.head(10).iterrows():

        symbol = r.get("symbol", "?")
        score = r.get("score_buy", 0)
        slope = r.get("slope", 0)
        mtf = r.get("mtf", 0)

        logger.info(
            "%s  score=%s  slope=%.4f  mtf=%.4f",
            symbol,
            score,
            slope,
            mtf
        )


# ============================================================
# public API
# ============================================================

def print_market_summary():

    try:

        now = dt.datetime.now()

        logger.info("")
        logger.info("================================================")
        logger.info(" MARKET SUMMARY SNAPSHOT %s", now)
        logger.info("================================================")

        _print_push_summary()
        _print_ranking_summary()

    except Exception as e:

        logger.exception("[MONITOR] summary print error: %s", e)