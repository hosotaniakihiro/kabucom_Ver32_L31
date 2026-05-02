# ============================================================
# File   : core/startup/closed_day_display.py
# Version: REV1.0-CLOSED-DAY-DISPLAY
# ------------------------------------------------------------
# 【概要】
#   closed-day 表示データ選択と表示実行を startup.py から分離
#
# 【主な機能】
#   - rebuild_closed_day_summaries_all の結果を優先
#   - summary_history_cache の最新行を優先利用
#   - merged summary / push_summary_cache fallback
#   - summary DB latest fallback
#   - print_latest_bar / print_summary_top10 実行
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from global_state import global_data
from core.startup.display_debug import log_display_input
from core.startup.summary_runtime import rebuild_closed_day_summaries_all
from core.startup.closed_day_db import normalize_closed_day_db_df, get_latest_db_summary

try:
    from core.startup.merged_summary_access import get_summary_history_safe
except Exception:
    get_summary_history_safe = None

from trading.summary.summary_printer import (
    print_latest_bar,
    print_summary_top10,
)

logger = logging.getLogger(__name__)


def latest_per_symbol(df: pd.DataFrame, tf: int) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        x = df.copy()

        if "datetime" not in x.columns:
            x = normalize_closed_day_db_df(x, tf)

        if "symbol" in x.columns:
            x["symbol"] = x["symbol"].astype(str).str.replace(".0", "", regex=False).str.strip()

        if "symbol" not in x.columns:
            return x.reset_index(drop=True)

        if "datetime" in x.columns:
            x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = (
                x.sort_values(["symbol", "datetime"], kind="stable")
                .drop_duplicates(subset=["symbol"], keep="last")
                .reset_index(drop=True)
            )
        else:
            x = x.drop_duplicates(subset=["symbol"], keep="last").reset_index(drop=True)

        return x
    except Exception:
        logger.exception("[CLOSED DAY PICK] latest per symbol failed tf=%s", tf)
        return df


def get_summary_history_latest(tf: int) -> pd.DataFrame:
    try:
        if not callable(get_summary_history_safe):
            return pd.DataFrame()

        hist = get_summary_history_safe(tf)
        if isinstance(hist, pd.DataFrame) and not hist.empty:
            latest = latest_per_symbol(hist, tf)
            logger.info(
                "[CLOSED DAY PICK] tf=%s source=summary_history_cache rows=%s latest_rows=%s",
                tf,
                len(hist),
                len(latest),
            )
            return latest

        return pd.DataFrame()
    except Exception:
        logger.debug("[CLOSED DAY PICK] summary history latest failed tf=%s", tf, exc_info=True)
        return pd.DataFrame()


def pick_closed_day_display_df(tf: int, rebuilt_map: dict) -> pd.DataFrame:
    """
    表示優先順位:
      1) rebuild_closed_day_summaries_all の結果
      2) summary_history_cache の最新行
      3) global_data.get_merged_summary(tf, source='push')
      4) push_summary_cache
      5) summary DB 最終確定足
    """
    try:
        df = rebuilt_map.get(tf)
        if isinstance(df, pd.DataFrame) and not df.empty:
            logger.info("[CLOSED DAY PICK] tf=%s source=rebuilt_map rows=%s", tf, len(df))
            return df

        hist_latest = get_summary_history_latest(tf)
        if isinstance(hist_latest, pd.DataFrame) and not hist_latest.empty:
            logger.info("[CLOSED DAY PICK] tf=%s source=summary_history_latest rows=%s", tf, len(hist_latest))
            return hist_latest

        try:
            merged_df = global_data.get_merged_summary(tf=tf, source="push")
        except Exception:
            merged_df = pd.DataFrame()

        if isinstance(merged_df, pd.DataFrame) and not merged_df.empty:
            logger.info("[CLOSED DAY PICK] tf=%s source=merged_summary_push rows=%s", tf, len(merged_df))
            return merged_df

        try:
            push_cache_df = global_data.get_push_summary(tf)
        except Exception:
            push_cache_df = None

        if isinstance(push_cache_df, pd.DataFrame) and not push_cache_df.empty:
            logger.info("[CLOSED DAY PICK] tf=%s source=push_summary_cache rows=%s", tf, len(push_cache_df))
            return push_cache_df

        db_df = get_latest_db_summary(tf)
        if isinstance(db_df, pd.DataFrame) and not db_df.empty:
            logger.info("[CLOSED DAY PICK] tf=%s source=summary_db_latest rows=%s", tf, len(db_df))
            return db_df

        return pd.DataFrame()

    except Exception:
        logger.exception("[CLOSED DAY PICK] failed tf=%s", tf)
        return pd.DataFrame()


def display_closed_day_summary_priority() -> None:
    logger.info("📅 Market closed → displaying last trading day summary (lightweight priority)")

    try:
        rebuilt_map = rebuild_closed_day_summaries_all(
            lightweight=True,
            limit_symbols=300,
        )

        if not isinstance(rebuilt_map, dict):
            logger.warning("⚠ Closed-day rebuilt_map is not dict -> %r", type(rebuilt_map))
            rebuilt_map = {}

        for tf in (1, 3, 5):
            df = pick_closed_day_display_df(tf, rebuilt_map)

            if df is None or df.empty:
                logger.warning("⚠ No summary data for %smin", tf)
                continue

            log_display_input(tf, df)
            print_latest_bar(tf)
            print_summary_top10(df=df, interval=tf)

    except Exception:
        logger.exception("❌ Closed-day summary display failed")


__all__ = [
    "latest_per_symbol",
    "get_summary_history_latest",
    "pick_closed_day_display_df",
    "display_closed_day_summary_priority",
]