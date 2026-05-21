# ============================================================
# File   : scheduler_jobs/summary/safe_io.py
# Version: PRODUCTION-STABLE-SUMMARY-SAFE-IO-V1.3-ENRICH-BEFORE-DISPLAY
# ------------------------------------------------------------
# 【概要】
#   summary DB保存 / PUSH表示 / RANKING表示の安全ラッパー。
#
# V1.3:
#   - display_push_summary_safe / display_ranking_summary_safe の表示前に
#     trading.summary.controller_enrich.enrich_summary_latest() を通す。
#   - scheduler_jobs.summary 経路の表示でも daily MTF / ranking_score を反映する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from .cache_writer import save_merged_summary
from .display_runner import display_push_summary, display_ranking_summary
from .time_utils import resolve_display_slot
from .runner_utils import df_rows, is_nonempty_df, log_df_state

from trading.summary.filters.liquidity_filter import (
    filter_liquid_summary_for_display,
    log_liquidity_profile,
)

logger = logging.getLogger(__name__)


def _enrich_for_display(df: pd.DataFrame, interval: int, source: str, context: str) -> pd.DataFrame:
    """scheduler_jobs.summary 経路でも ranking / daily MTF を表示直前に付与する。"""
    if not is_nonempty_df(df):
        return df
    try:
        from trading.summary.controller_enrich import enrich_summary_latest

        out = enrich_summary_latest(
            df,
            interval=int(interval),
            context=f"scheduler-{source.lower()}-{context}",
        )
        try:
            logger.info(
                "[summary.runners] enrich_for_display source=%s interval=%s context=%s rows=%s cols=%s",
                source,
                interval,
                context,
                len(out) if isinstance(out, pd.DataFrame) else None,
                len(out.columns) if isinstance(out, pd.DataFrame) else None,
            )
        except Exception:
            pass
        return out
    except Exception:
        logger.exception(
            "[summary.runners] enrich_for_display failed source=%s interval=%s context=%s",
            source,
            interval,
            context,
        )
        return df


# ============================================================
# 保存安全ラッパー
# ============================================================

def save_summary_safe(df: pd.DataFrame, interval: int, source: str) -> bool:
    try:
        rows = df_rows(df)

        if not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] save_summary skipped source=%s interval=%s reason=empty_df rows=%d",
                source,
                interval,
                rows,
            )
            return False

        logger.info(
            "[summary.runners] save_summary start source=%s interval=%s rows=%d",
            source,
            interval,
            rows,
        )

        save_merged_summary(df, interval, source=source)

        logger.info(
            "[summary.runners] save_summary success source=%s interval=%s rows=%d",
            source,
            interval,
            rows,
        )
        return True

    except Exception:
        logger.exception(
            "[summary.runners] save_summary failed source=%s interval=%s",
            source,
            interval,
        )
        return False


# ============================================================
# PUSHサマリー表示安全ラッパー
# ============================================================

def display_push_summary_safe(df: pd.DataFrame, interval: int, now: dt.datetime) -> bool:
    try:
        if not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] display_push_summary skipped interval=%s reason=empty_df now=%s",
                interval,
                now,
            )
            return False

        _, slot_dt = resolve_display_slot(interval=interval, now=now)

        df = _enrich_for_display(df, interval, "PUSH", "before-liquidity")

        logger.info(
            "[summary.runners] display_push_summary start interval=%s rows=%d now=%s slot=%s",
            interval,
            len(df),
            now,
            slot_dt,
        )

        log_df_state("display_push_input_before_liquidity", interval, df)

        log_liquidity_profile(
            df,
            interval=interval,
            source="PUSH",
            label="display_push_before_filter",
        )

        display_df = filter_liquid_summary_for_display(
            df,
            interval=interval,
            source="PUSH",
        )

        if not is_nonempty_df(display_df):
            logger.warning(
                "[summary.runners] display_push_summary skipped interval=%s "
                "reason=empty_after_liquidity_filter before_rows=%d now=%s slot=%s",
                interval,
                len(df),
                now,
                slot_dt,
            )
            return False

        display_df = _enrich_for_display(display_df, interval, "PUSH", "after-liquidity")

        log_df_state("display_push_input_after_liquidity", interval, display_df)

        log_liquidity_profile(
            display_df,
            interval=interval,
            source="PUSH",
            label="display_push_after_filter",
        )

        display_push_summary(display_df, interval, now=now)

        logger.info(
            "[summary.runners] display_push_summary success interval=%s before_rows=%d after_rows=%d now=%s slot=%s",
            interval,
            len(df),
            len(display_df),
            now,
            slot_dt,
        )
        return True

    except Exception:
        logger.exception(
            "[summary.runners] display_push_summary failed interval=%s now=%s",
            interval,
            now,
        )
        return False


# ============================================================
# RANKINGサマリー表示安全ラッパー
# ============================================================

def display_ranking_summary_safe(df: pd.DataFrame, interval: int, now: dt.datetime) -> bool:
    try:
        if not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] display_ranking_summary skipped interval=%s reason=empty_df now=%s",
                interval,
                now,
            )
            return False

        _, slot_dt = resolve_display_slot(interval=interval, now=now)

        df = _enrich_for_display(df, interval, "RANKING", "before-liquidity")

        logger.info(
            "[summary.runners] display_ranking_summary start interval=%s rows=%d now=%s slot=%s",
            interval,
            len(df),
            now,
            slot_dt,
        )

        log_df_state("display_ranking_input_before_liquidity", interval, df)

        log_liquidity_profile(
            df,
            interval=interval,
            source="RANKING",
            label="display_ranking_before_filter",
        )

        display_df = filter_liquid_summary_for_display(
            df,
            interval=interval,
            source="RANKING",
        )

        if not is_nonempty_df(display_df):
            logger.warning(
                "[summary.runners] display_ranking_summary skipped interval=%s "
                "reason=empty_after_liquidity_filter before_rows=%d now=%s slot=%s",
                interval,
                len(df),
                now,
                slot_dt,
            )
            return False

        display_df = _enrich_for_display(display_df, interval, "RANKING", "after-liquidity")

        log_df_state("display_ranking_input_after_liquidity", interval, display_df)

        log_liquidity_profile(
            display_df,
            interval=interval,
            source="RANKING",
            label="display_ranking_after_filter",
        )

        display_ranking_summary(display_df, interval, now=now)

        logger.info(
            "[summary.runners] display_ranking_summary success interval=%s before_rows=%d after_rows=%d now=%s slot=%s",
            interval,
            len(df),
            len(display_df),
            now,
            slot_dt,
        )
        return True

    except Exception:
        logger.exception(
            "[summary.runners] display_ranking_summary failed interval=%s now=%s",
            interval,
            now,
        )
        return False
