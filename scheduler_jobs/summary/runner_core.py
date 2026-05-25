#====================================================================================================
# scheduler_jobs/summary/runner_core.py
#====================================================================================================
# ============================================================
# File   : scheduler_jobs/summary/runner_core.py
# Version: PRODUCTION-STABLE-SUMMARY-RUNNER-CORE-V1.5-ENTRY-BEFORE-DISPLAY
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
#   - 入力生成ルートは PUSH と RANKING で分離する
#   - 保存/表示/AI hook の出口パイプラインは同じ形に揃える
#   - PUSH は source="SUMMARY"
#   - RANKING は source="RANKING"
#
# REV1.3:
#   - summary_ai_entry_hook_v20 を使用
#   - PUSH/RANKINGとも BUY TOP20 + SELL TOP20 をAIへ渡す
#   - RANKINGではAI前段のtonosama/slopeフィルタをhook側でOFFにする
#
# REV1.4:
#   - サマリー計算は main.py / main_database.py 両方で実行可能
#   - DB保存は環境変数 AUTOSTOCK_SUMMARY_SAVE_OWNER で owner を制御
#   - owner=database の場合、data collector/main_database 側だけ保存
#   - main.py 側は計算・表示・AI/entryを継続し、DB保存だけskip可能
#
# REV1.5:
#   - 表示/Discord送信が重く、SUMMARY PARALLEL timeout でAI entryまで到達しない問題を修正
#   - AI entry hook を display より前に実行する
#   - これにより SUMMARY TOP10 表示やDiscordが遅れても、AI_OK→発注処理を先に進める
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
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
from .runner_utils import call_runner_with_optional_now, log_df_state
from .safe_io import (
    save_summary_safe,
    display_push_summary_safe,
    display_ranking_summary_safe,
)
from .summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
from .time_utils import (
    now_naive,
    floor_to_interval,
    is_market_session,
)

logger = logging.getLogger(__name__)


# ============================================================
# save owner gate
# ============================================================

def _env_flag_value(name: str) -> str:
    try:
        return str(os.getenv(name, "")).strip().lower()
    except Exception:
        return ""


def _env_true(name: str) -> bool:
    raw = _env_flag_value(name)
    return raw in ("1", "true", "yes", "on", "enable", "enabled")


def _env_false(name: str) -> bool:
    raw = _env_flag_value(name)
    return raw in ("0", "false", "no", "off", "disable", "disabled")


def _is_database_process() -> bool:
    """
    main_database.py / data_collectors_runner 配下のプロセス判定。
    """
    return any(
        _env_true(name)
        for name in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        )
    )


def _summary_save_enabled() -> bool:
    """
    サマリーDB保存の owner をプロセスごとに切り替える。

    環境変数:
      AUTOSTOCK_SUMMARY_SAVE_MODE=disabled / calculate_only / no_save
        -> 常に保存しない

      AUTOSTOCK_SUMMARY_SAVE_MODE=enabled / save
        -> 常に保存する

      AUTOSTOCK_SUMMARY_SAVE_OWNER=database
        -> main_database.py / data collectors 側だけ保存する

      AUTOSTOCK_SUMMARY_SAVE_OWNER=main
        -> main.py 側だけ保存する

      未指定:
        -> 従来互換として保存する
    """
    mode = _env_flag_value("AUTOSTOCK_SUMMARY_SAVE_MODE")
    if mode in ("disabled", "disable", "calculate_only", "calc_only", "no_save", "skip", "off"):
        return False
    if mode in ("enabled", "enable", "save", "on"):
        return True

    owner = _env_flag_value("AUTOSTOCK_SUMMARY_SAVE_OWNER")
    if owner in ("database", "main_database", "data_collector", "data_collectors", "db"):
        return _is_database_process()
    if owner in ("main", "main.py", "realtime", "entry"):
        return not _is_database_process()
    if owner in ("both", "all", "any"):
        return True
    if owner in ("none", "off", "disabled", "no_save"):
        return False

    # owner未指定時は従来互換: 保存する
    return True


def _save_summary_if_owner(df: pd.DataFrame, interval: int, *, source: str) -> None:
    if _summary_save_enabled():
        logger.info(
            "[summary.runners] DB save enabled interval=%s source=%s owner=%s db_process=%s",
            interval,
            source,
            os.getenv("AUTOSTOCK_SUMMARY_SAVE_OWNER", ""),
            _is_database_process(),
        )
        save_summary_safe(df, interval, source=source)
        return

    logger.info(
        "[summary.runners] DB save skipped interval=%s source=%s owner=%s mode=%s db_process=%s "
        "reason=summary_save_owner_gate",
        interval,
        source,
        os.getenv("AUTOSTOCK_SUMMARY_SAVE_OWNER", ""),
        os.getenv("AUTOSTOCK_SUMMARY_SAVE_MODE", ""),
        _is_database_process(),
    )


def _run_push_ai_entry_before_display(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> None:
    """
    表示/Discord送信より先にAI entryを走らせる。
    表示処理が重い場合でも、エントリー機会を先に処理するため。
    """
    if run_entry and interval in (1, 3, 5):
        logger.info(
            "[summary.runners] push AI entry requested before display interval=%s now=%s source=SUMMARY hook=v20",
            interval,
            now,
        )
        run_summary_ai_entry_safe(interval=interval, now=now, df=df, source="SUMMARY")
    else:
        logger.info(
            "[summary.runners] summary AI entry skipped interval=%s run_entry=%s reason=%s",
            interval,
            run_entry,
            "interval_not_enabled" if interval not in (1, 3, 5) else "run_entry_false",
        )


def _run_ranking_ai_entry_before_display(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> None:
    if run_entry and interval in (1, 3, 5) and is_market_session(now):
        logger.info(
            "[summary.runners] ranking AI entry requested before display interval=%s now=%s source=RANKING hook=v20",
            interval,
            now,
        )
        run_summary_ai_entry_safe(interval=interval, now=now, df=df, source="RANKING")
    else:
        logger.info(
            "[summary.runners] ranking AI entry skipped interval=%s run_entry=%s in_session=%s reason=%s",
            interval,
            run_entry,
            is_market_session(now),
            "interval_not_enabled" if interval not in (1, 3, 5) else "run_entry_false_or_closed_market",
        )


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
    run_entry: bool = True,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_1m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_ranking_summary(1, display=display, now=now, run_entry=run_entry)


def job_ranking_3m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = True,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_3m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_ranking_summary(3, display=display, now=now, run_entry=run_entry)


def job_ranking_5m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = True,
) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_5m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_ranking_summary(5, display=display, now=now, run_entry=run_entry)


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
    interval = int(interval)
    now = (now or now_naive()).replace(microsecond=0)

    logger.info(
        "[summary.runners] job_summary(PUSH) start interval=%s display=%s run_entry=%s "
        "now=%s slot=%s in_session=%s extra_keys=%s save_enabled=%s db_process=%s",
        interval,
        display,
        run_entry,
        now,
        floor_to_interval(now, interval),
        is_market_session(now),
        sorted(list(kwargs.keys())),
        _summary_save_enabled(),
        _is_database_process(),
    )

    if not is_market_session(now):
        logger.info(
            "[summary.runners] market closed/lunch interval=%s now=%s slot=%s "
            "-> display latest persisted summary / fallback / rebuild if empty",
            interval,
            now,
            floor_to_interval(now, interval),
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

    _save_summary_if_owner(df, interval, source="push")
    log_job_result("job_summary(PUSH)", interval, df, meta)

    # REV1.5: エントリー機会を優先するため、表示/Discordより先にAI entryを実行する。
    _run_push_ai_entry_before_display(df, interval, now, run_entry)

    if display:
        display_push_summary_safe(df, interval, now=now)
    else:
        logger.info("[summary.runners] display skipped interval=%s source=push reason=display_false", interval)

    return df


# ============================================================
# RANKING summary job
# ============================================================

def job_ranking_summary(
    interval: int,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    run_entry: bool = True,
    **kwargs,
) -> pd.DataFrame:
    interval = int(interval)
    now = (now or now_naive()).replace(microsecond=0)

    logger.info(
        "[summary.runners] job_ranking_summary(RANKING) start interval=%s display=%s run_entry=%s "
        "now=%s slot=%s in_session=%s extra_keys=%s save_enabled=%s db_process=%s",
        interval,
        display,
        run_entry,
        now,
        floor_to_interval(now, interval),
        is_market_session(now),
        sorted(list(kwargs.keys())),
        _summary_save_enabled(),
        _is_database_process(),
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

    _save_summary_if_owner(df, interval, source="ranking")
    log_job_result("job_ranking_summary(RANKING)", interval, df, meta)

    # REV1.5: RANKING由来もAI entryを表示より先に実行する。
    _run_ranking_ai_entry_before_display(df, interval, now, run_entry)

    if display:
        display_ranking_summary_safe(df, interval, now=now)
    else:
        logger.info("[summary.runners] display skipped interval=%s source=ranking reason=display_false", interval)

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
    run_entry: bool = True,
    **kwargs,
) -> pd.DataFrame:
    return job_ranking_summary(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)


__all__ = [
    "job_1m",
    "job_3m",
    "job_5m",
    "job_summary",
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "job_ranking_summary",
    "run_push_summary_job",
    "run_ranking_summary_job",
]
