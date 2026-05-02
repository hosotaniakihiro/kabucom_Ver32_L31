# ============================================================
# File   : trading/summary/ranking/runner.py
# Ver    : PRODUCTION-STABLE-RANKING-SUMMARY-RUNNER-V1.0
#          -RANKING-ONLY
#          -THIN-FACADE-RUNNER
#          -DISPLAY-SEPARATED
# ------------------------------------------------------------
# ✔ RANKING由来サマリー専用 runner
# ✔ ranking 本体は trading.ranking.ranking_summary_engine を利用
# ✔ PUSH系は一切参照しない
# ✔ 実行入口を RANKING のみに限定
# ✔ 1m / 3m / 5m の job 提供
# ✔ runner が now を受け取れる時だけ now を伝搬
# ✔ display 到達ログを強化
# ✔ fallback 前後の行数を可視化
# ✔ 定時計算は scheduler 側の time-locked 制御前提
# ✔ 表示は RANKING display 側で session anchor に従って固定表示
# ============================================================

from __future__ import annotations

import datetime as dt
import inspect
import logging
from typing import Any, Optional

import pandas as pd

from trading.ranking.ranking_summary_engine import run_ranking_summary_job as _core_run_ranking_summary_job

from .cache_writer import save_ranking_summary
from .display_prepare import normalize_df, latest_dt_str, symbols_count, clamp_future_rows
from .display import display_ranking_summary
from .fallback_loader import fallback_ranking_summary_df
from .guards import looks_uncomputed_ranking_df
from .time_utils import (
    now_naive,
    resolve_target_intervals,
    floor_to_interval,
    is_market_session,
    resolve_display_slot,
)

logger = logging.getLogger(__name__)


# ============================================================
# basic helpers
# ============================================================

def _safe_cols(df: Any) -> list[str]:
    try:
        if isinstance(df, pd.DataFrame):
            return list(df.columns)
    except Exception:
        pass
    return []


def _log_df_state(label: str, interval: int, df: pd.DataFrame) -> None:
    try:
        logger.info(
            "[ranking.runner] %s interval=%s rows=%d symbols=%d cols=%d has_datetime=%s latest_dt=%s columns=%s",
            label,
            interval,
            len(df) if isinstance(df, pd.DataFrame) else 0,
            symbols_count(df) if isinstance(df, pd.DataFrame) else 0,
            len(df.columns) if isinstance(df, pd.DataFrame) else 0,
            ("datetime" in df.columns) if isinstance(df, pd.DataFrame) else False,
            latest_dt_str(df) if isinstance(df, pd.DataFrame) else None,
            _safe_cols(df)[:20],
        )
    except Exception:
        logger.exception("[ranking.runner] %s state log failed interval=%s", label, interval)


def _runner_accepts_now(runner: Any) -> bool:
    try:
        sig = inspect.signature(runner)
        params = sig.parameters

        if "now" in params:
            return True

        for p in params.values():
            if p.kind == inspect.Parameter.VAR_KEYWORD:
                return True

        return False
    except Exception:
        logger.exception(
            "[ranking.runner] failed to inspect runner signature runner=%s",
            getattr(runner, "__name__", repr(runner)),
        )
        return False


def _call_runner_with_optional_now(
    runner: Any,
    *,
    interval: int,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> Any:
    call_kwargs = dict(kwargs)
    call_kwargs["interval"] = interval

    accepts_now = _runner_accepts_now(runner)
    if now is not None and accepts_now:
        call_kwargs["now"] = now

    logger.info(
        "[ranking.runner] runner call interval=%s runner=%s accepts_now=%s passed_now=%s extra_keys=%s",
        interval,
        getattr(runner, "__name__", repr(runner)),
        accepts_now,
        ("now" in call_kwargs),
        sorted([k for k in call_kwargs.keys() if k != "interval"]),
    )

    return runner(**call_kwargs)


# ============================================================
# normalize runner output
# ============================================================

def normalize_runner_output(result: Any) -> tuple[pd.DataFrame, dict]:
    meta: dict = {}

    logger.info(
        "[ranking.runner] normalize_runner_output start result_type=%s",
        type(result).__name__,
    )

    if result is None:
        logger.warning("[ranking.runner] normalize_runner_output: result is None")
        return pd.DataFrame(), meta

    if isinstance(result, pd.DataFrame):
        logger.info(
            "[ranking.runner] normalize_runner_output: dataframe rows=%d cols=%d",
            len(result),
            len(result.columns),
        )
        return result.copy(), meta

    if isinstance(result, tuple):
        logger.info(
            "[ranking.runner] normalize_runner_output: tuple len=%d",
            len(result),
        )
        if len(result) >= 1 and isinstance(result[0], pd.DataFrame):
            if len(result) >= 2 and isinstance(result[1], dict):
                meta = result[1].copy()
            logger.info(
                "[ranking.runner] normalize_runner_output: tuple[0] dataframe rows=%d meta_keys=%s",
                len(result[0]),
                sorted(list(meta.keys())) if isinstance(meta, dict) else [],
            )
            return result[0].copy(), meta
        logger.warning("[ranking.runner] normalize_runner_output: tuple but dataframe not found")
        return pd.DataFrame(), meta

    if isinstance(result, dict):
        meta = result.copy()
        logger.info(
            "[ranking.runner] normalize_runner_output: dict keys=%s",
            sorted(list(result.keys())),
        )
        for key in (
            "result_df",
            "merged_df",
            "df",
            "summary_df",
            "output_df",
            "display_df",
            "latest_df",
            "latest_summary_df",
        ):
            val = result.get(key)
            if isinstance(val, pd.DataFrame):
                logger.info(
                    "[ranking.runner] normalize_runner_output: dataframe found in dict key=%s rows=%d",
                    key,
                    len(val),
                )
                return val.copy(), meta
        logger.warning("[ranking.runner] normalize_runner_output: dict but dataframe key not found")
        return pd.DataFrame(), meta

    logger.warning(
        "[ranking.runner] normalize_runner_output: unsupported result_type=%s",
        type(result).__name__,
    )
    return pd.DataFrame(), meta


def log_job_result(label: str, interval: int, df: pd.DataFrame, meta: Optional[dict] = None) -> None:
    meta = meta or {}
    logger.info(
        "[ranking.runner] %s done interval=%s rows=%d symbols=%d latest_dt=%s meta_keys=%s",
        label,
        interval,
        len(df) if isinstance(df, pd.DataFrame) else 0,
        symbols_count(df),
        latest_dt_str(df),
        sorted(list(meta.keys())) if isinstance(meta, dict) else [],
    )


# ============================================================
# save / display wrappers
# ============================================================

def _save_ranking_summary_safe(df: pd.DataFrame, interval: int, now: Optional[dt.datetime] = None) -> None:
    try:
        logger.info(
            "[ranking.runner] save_ranking_summary start interval=%s rows=%d",
            interval,
            len(df) if isinstance(df, pd.DataFrame) else 0,
        )
        save_ranking_summary(df, interval, now=now)
        logger.info(
            "[ranking.runner] save_ranking_summary success interval=%s rows=%d",
            interval,
            len(df) if isinstance(df, pd.DataFrame) else 0,
        )
    except Exception:
        logger.exception(
            "[ranking.runner] save_ranking_summary failed interval=%s",
            interval,
        )


def _display_ranking_summary_safe(df: pd.DataFrame, interval: int, now: dt.datetime) -> None:
    try:
        _, slot_dt = resolve_display_slot(interval=interval, now=now)
        logger.info(
            "[ranking.runner] display_ranking_summary start interval=%s rows=%d now=%s slot=%s",
            interval,
            len(df) if isinstance(df, pd.DataFrame) else 0,
            now,
            slot_dt,
        )
        _log_df_state("display_ranking_input", interval, df)
        display_ranking_summary(df, interval=interval, now=now)
        logger.info(
            "[ranking.runner] display_ranking_summary success interval=%s rows=%d now=%s slot=%s",
            interval,
            len(df) if isinstance(df, pd.DataFrame) else 0,
            now,
            slot_dt,
        )
    except Exception:
        logger.exception(
            "[ranking.runner] display_ranking_summary failed interval=%s now=%s",
            interval,
            now,
        )


# ============================================================
# public jobs
# ============================================================

def job_ranking_1m(display: bool = True, now: Optional[dt.datetime] = None, **kwargs) -> pd.DataFrame:
    logger.info("[ranking.runner] job_ranking_1m start display=%s now=%s", display, now)
    return job_ranking_summary(1, display=display, now=now, **kwargs)


def job_ranking_3m(display: bool = True, now: Optional[dt.datetime] = None, **kwargs) -> pd.DataFrame:
    logger.info("[ranking.runner] job_ranking_3m start display=%s now=%s", display, now)
    return job_ranking_summary(3, display=display, now=now, **kwargs)


def job_ranking_5m(display: bool = True, now: Optional[dt.datetime] = None, **kwargs) -> pd.DataFrame:
    logger.info("[ranking.runner] job_ranking_5m start display=%s now=%s", display, now)
    return job_ranking_summary(5, display=display, now=now, **kwargs)


def job_ranking_summary(
    interval: int,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    """
    RANKING由来サマリー専用ジョブ。
    push 系の df は一切扱わない。
    """
    interval = int(interval)
    now = (now or now_naive()).replace(microsecond=0)

    logger.info(
        "[ranking.runner] job_ranking_summary start interval=%s display=%s now=%s slot=%s in_session=%s extra_keys=%s",
        interval,
        display,
        now,
        floor_to_interval(now, interval),
        is_market_session(now),
        sorted(list(kwargs.keys())),
    )

    runner = _core_run_ranking_summary_job
    if not callable(runner):
        raise RuntimeError("ranking summary runner is not available")

    logger.info(
        "[ranking.runner] ranking runner resolved interval=%s runner=%s",
        interval,
        getattr(runner, "__name__", repr(runner)),
    )

    # ranking 本体は announce/use_discord を受ける
    if "announce" not in kwargs:
        kwargs["announce"] = False
    if "use_discord" not in kwargs:
        kwargs["use_discord"] = False

    result = _call_runner_with_optional_now(
        runner,
        interval=interval,
        now=now,
        **kwargs,
    )
    df, meta = normalize_runner_output(result)

    _log_df_state("ranking_after_normalize_runner_output", interval, df)

    df = normalize_df(df)
    _log_df_state("ranking_after_normalize_df", interval, df)

    if not df.empty and looks_uncomputed_ranking_df(df):
        logger.warning(
            "[ranking.runner] runner returned uncomputed RANKING df interval=%s latest_dt=%s -> trying fallback",
            interval,
            latest_dt_str(df),
        )
        df = pd.DataFrame()

    if df.empty:
        logger.warning(
            "[ranking.runner] runner returned empty RANKING interval=%s -> trying ranking-only fallback",
            interval,
        )
        df = fallback_ranking_summary_df(interval, now=now)
        _log_df_state("ranking_after_fallback_ranking_summary_df", interval, df)

    df = normalize_df(df)
    _log_df_state("ranking_after_second_normalize_df", interval, df)

    before_clamp_rows = len(df)
    df = clamp_future_rows(df, interval=interval, now=now)
    logger.info(
        "[ranking.runner] clamp_future_rows interval=%s source=ranking before=%d after=%d now=%s",
        interval,
        before_clamp_rows,
        len(df),
        now,
    )
    _log_df_state("ranking_after_clamp_future_rows", interval, df)

    _save_ranking_summary_safe(df, interval, now=now)
    log_job_result("job_ranking_summary", interval, df, meta)

    if display:
        _display_ranking_summary_safe(df, interval, now=now)
    else:
        logger.info("[ranking.runner] display skipped interval=%s source=ranking reason=display_false", interval)

    return df


# ============================================================
# time-locked execution
# ============================================================

def run_time_locked_jobs(
    *,
    now: Optional[dt.datetime] = None,
    display: bool = True,
) -> dict[int, pd.DataFrame]:
    now = (now or now_naive()).replace(microsecond=0)
    targets = resolve_target_intervals(now)

    logger.info(
        "[ranking.runner] time-locked tick now=%s targets=%s display=%s in_session=%s",
        now,
        targets,
        display,
        is_market_session(now),
    )

    out: dict[int, pd.DataFrame] = {}

    if not targets:
        logger.info(
            "[ranking.runner] time-locked tick skipped now=%s reason=outside_market_session_or_no_target",
            now,
        )
        return out

    for interval in targets:
        try:
            logger.info(
                "[ranking.runner] time-locked ranking begin interval=%s now=%s",
                interval,
                now,
            )
            out[interval] = job_ranking_summary(interval, display=display, now=now)
            logger.info(
                "[ranking.runner] time-locked ranking end interval=%s rows=%d",
                interval,
                len(out[interval]),
            )
        except Exception:
            logger.exception(
                "[ranking.runner] time-locked ranking job failed interval=%s now=%s",
                interval,
                now,
            )
            out[interval] = pd.DataFrame()

    return out


def run_ranking_summary_job(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    return job_ranking_summary(int(interval), display=display, now=now, **kwargs)


__all__ = [
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "job_ranking_summary",
    "run_time_locked_jobs",
    "run_ranking_summary_job",
]