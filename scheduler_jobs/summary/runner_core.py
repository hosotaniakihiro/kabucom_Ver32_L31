
#====================================================================================================
# scheduler_jobs/summary/runner_core.py
#====================================================================================================
# ============================================================
# File   : scheduler_jobs/summary/runner_core.py
# Version: PRODUCTION-STABLE-SUMMARY-RUNNER-CORE-V1.1-CLOSED-REBUILD-AWARE
# ------------------------------------------------------------
# 【概要】
#   PUSH / RANKING サマリーの実行本体。
#
# 【主な機能】
#   - job_summary
#   - job_ranking_summary
#   - job_1m / job_3m / job_5m
#   - job_ranking_1m / job_ranking_3m / job_ranking_5m
#   - run_push_summary_job / run_ranking_summary_job
#
# 【分離方針】
#   - 保存/表示は safe_io に委譲
#   - 時間外表示は closed_market_display に委譲
#   - AI entry は summary_ai_entry_hook に委譲
#   - normalizeは output_normalizer に委譲
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from .closed_market_display import display_closed_market_push_summary
from .dependencies import (
    resolve_push_summary_runner,
    resolve_ranking_summary_runner,
)
from .display_prepare import normalize_df, latest_dt_str, clamp_future_rows
from .fallback_loader import (
    fallback_push_summary_df,
    fallback_ranking_summary_df,
    filter_push_like_rows,
)
from .output_normalizer import normalize_runner_output, log_job_result
from .quality_guards import looks_uncomputed_push_df, looks_uncomputed_ranking_df
from .runner_utils import call_runner_with_optional_now, is_nonempty_df, log_df_state
from .safe_io import (
    save_summary_safe,
    display_push_summary_safe,
    display_ranking_summary_safe,
)
from .summary_ai_entry_hook import run_summary_ai_entry_safe
from .time_utils import (
    now_naive,
    floor_to_interval,
    is_market_session,
    resolve_display_slot,
)

logger = logging.getLogger(__name__)


# ============================================================
# public shortcut jobs
# ============================================================

def job_1m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = True,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_1m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_summary(1, display=display, now=now, run_entry=run_entry)


def job_3m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = True,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_3m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_summary(3, display=display, now=now, run_entry=run_entry)


def job_5m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = True,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_5m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_summary(5, display=display, now=now, run_entry=run_entry)


def job_ranking_1m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_1m start display=%s now=%s", display, now)
    return job_ranking_summary(1, display=display, now=now)


def job_ranking_3m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_3m start display=%s now=%s", display, now)
    return job_ranking_summary(3, display=display, now=now)


def job_ranking_5m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_5m start display=%s now=%s", display, now)
    return job_ranking_summary(5, display=display, now=now)


# ============================================================
# PUSH summary job
# ============================================================

def job_summary(
    interval: int,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = True,
    **kwargs,
) -> pd.DataFrame:
    """
    PUSH由来サマリー専用ジョブ。
    ranking 系の df をここへ混ぜない。
    """
    interval = int(interval)
    now = (now or now_naive()).replace(microsecond=0)

    logger.info(
        "[summary.runners] job_summary(PUSH) start interval=%s display=%s run_entry=%s "
        "now=%s slot=%s in_session=%s extra_keys=%s",
        interval,
        display,
        run_entry,
        now,
        floor_to_interval(now, interval),
        is_market_session(now),
        sorted(list(kwargs.keys())),
    )

    if not is_market_session(now):
        logger.info(
            "[summary.runners] market closed/lunch interval=%s now=%s slot=%s "
            "-> display latest persisted summary / fallback / rebuild if empty",
            interval,
            now,
            resolve_display_slot(now),
        )
        df = display_closed_market_push_summary(interval=interval, now=now)
        log_job_result("job_summary(PUSH-CLOSED)", interval, df, {})
        return df

    runner = resolve_push_summary_runner()
    if not callable(runner):
        raise RuntimeError("push summary runner is not available")

    logger.info(
        "[summary.runners] push runner resolved interval=%s runner=%s",
        interval,
        getattr(runner, "__name__", repr(runner)),
    )

    result = call_runner_with_optional_now(
        runner,
        interval=interval,
        now=now,
        **kwargs,
    )

    df, meta = normalize_runner_output(result)

    log_df_state("push_after_normalize_runner_output", interval, df)

    df = normalize_df(df)
    log_df_state("push_after_normalize_df", interval, df)

    before_filter_rows = len(df)
    df = filter_push_like_rows(df)

    logger.info(
        "[summary.runners] filter_push_like_rows interval=%s before=%d after=%d",
        interval,
        before_filter_rows,
        len(df),
    )
    log_df_state("push_after_filter_push_like_rows", interval, df)

    if not df.empty and looks_uncomputed_push_df(df):
        logger.warning(
            "[summary.runners] runner returned uncomputed PUSH df interval=%s latest_dt=%s -> trying fallback",
            interval,
            latest_dt_str(df),
        )
        df = pd.DataFrame()

    if df.empty:
        logger.warning(
            "[summary.runners] runner returned empty PUSH interval=%s -> trying push-only fallback from db/cache",
            interval,
        )
        df = fallback_push_summary_df(interval, now=now)
        log_df_state("push_after_fallback_push_summary_df", interval, df)

    df = normalize_df(df)
    log_df_state("push_after_second_normalize_df", interval, df)

    before_clamp_rows = len(df)
    df = clamp_future_rows(df, interval=interval, now=now)

    logger.info(
        "[summary.runners] clamp_future_rows interval=%s source=push before=%d after=%d now=%s",
        interval,
        before_clamp_rows,
        len(df),
        now,
    )
    log_df_state("push_after_clamp_future_rows", interval, df)

    if df.empty:
        logger.warning(
            "[summary.runners] job_summary(PUSH) empty after runner/fallback/clamp interval=%s now=%s "
            "-> skip save/display/entry",
            interval,
            now,
        )
        log_job_result("job_summary(PUSH-EMPTY)", interval, df, meta)
        return df

    save_summary_safe(df, interval, source="push")
    log_job_result("job_summary(PUSH)", interval, df, meta)

    if display:
        display_push_summary_safe(df, interval, now=now)
    else:
        logger.info("[summary.runners] display skipped interval=%s source=push reason=display_false", interval)

    # 時間内のみ、summary AI entry pipeline を安全起動
    if run_entry and interval in (1, 3, 5):
        run_summary_ai_entry_safe(interval=interval, now=now, df=df, source="SUMMARY")
    else:
        logger.info(
            "[summary.runners] summary AI entry skipped interval=%s run_entry=%s reason=%s",
            interval,
            run_entry,
            "interval_not_enabled" if interval not in (1, 3, 5) else "run_entry_false",
        )

    return df


# ============================================================
# RANKING summary job
# ============================================================

def job_ranking_summary(
    interval: int,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = False,
    **kwargs,
) -> pd.DataFrame:
    """
    ランキング由来サマリー専用ジョブ。
    push 系の df をここへ混ぜない。

    注意:
      RANKING summary からの AI entry はデフォルト無効。
      必要な場合は run_entry=True で明示する。
    """
    interval = int(interval)
    now = (now or now_naive()).replace(microsecond=0)

    logger.info(
        "[summary.runners] job_ranking_summary(RANKING) start interval=%s display=%s run_entry=%s "
        "now=%s slot=%s in_session=%s extra_keys=%s",
        interval,
        display,
        run_entry,
        now,
        floor_to_interval(now, interval),
        is_market_session(now),
        sorted(list(kwargs.keys())),
    )

    runner = resolve_ranking_summary_runner()
    if not callable(runner):
        raise RuntimeError("ranking summary runner is not available")

    logger.info(
        "[summary.runners] ranking runner resolved interval=%s runner=%s",
        interval,
        getattr(runner, "__name__", repr(runner)),
    )

    result = call_runner_with_optional_now(
        runner,
        interval=interval,
        now=now,
        **kwargs,
    )

    df, meta = normalize_runner_output(result)

    log_df_state("ranking_after_normalize_runner_output", interval, df)

    df = normalize_df(df)
    log_df_state("ranking_after_normalize_df", interval, df)

    if not df.empty and looks_uncomputed_ranking_df(df):
        logger.warning(
            "[summary.runners] ranking runner returned uncomputed RANKING df interval=%s latest_dt=%s "
            "-> trying fallback",
            interval,
            latest_dt_str(df),
        )
        df = pd.DataFrame()

    if df.empty:
        logger.warning(
            "[summary.runners] ranking runner returned empty interval=%s "
            "-> trying ranking-only fallback from cache/global_data",
            interval,
        )
        df = fallback_ranking_summary_df(interval, now=now)
        log_df_state("ranking_after_fallback_ranking_summary_df", interval, df)

    df = normalize_df(df)
    log_df_state("ranking_after_second_normalize_df", interval, df)

    before_clamp_rows = len(df)
    df = clamp_future_rows(df, interval=interval, now=now)

    logger.info(
        "[summary.runners] clamp_future_rows interval=%s source=ranking before=%d after=%d now=%s",
        interval,
        before_clamp_rows,
        len(df),
        now,
    )
    log_df_state("ranking_after_clamp_future_rows", interval, df)

    if df.empty:
        logger.warning(
            "[summary.runners] job_ranking_summary(RANKING) empty after runner/fallback/clamp interval=%s now=%s "
            "-> skip save/display/entry",
            interval,
            now,
        )
        log_job_result("job_ranking_summary(RANKING-EMPTY)", interval, df, meta)
        return df

    save_summary_safe(df, interval, source="ranking")
    log_job_result("job_ranking_summary(RANKING)", interval, df, meta)

    if display:
        display_ranking_summary_safe(df, interval, now=now)
    else:
        logger.info("[summary.runners] display skipped interval=%s source=ranking reason=display_false", interval)

    if run_entry and interval in (1, 3, 5) and is_market_session(now):
        logger.info(
            "[summary.runners] ranking AI entry requested interval=%s now=%s",
            interval,
            now,
        )
        run_summary_ai_entry_safe(interval=interval, now=now, df=df, source="RANKING")

    return df


# ============================================================
# compatibility aliases
# ============================================================

def run_push_summary_job(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = True,
    **kwargs,
) -> pd.DataFrame:
    return job_summary(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)


def run_ranking_summary_job(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = False,
    **kwargs,
) -> pd.DataFrame:
    return job_ranking_summary(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)


