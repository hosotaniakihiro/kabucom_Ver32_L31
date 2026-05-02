# ============================================================
# File   : scheduler_jobs/summary/ranking_summary.py
# Version: FIX-RECURSION-GUARD-V2
#          -NOW-PASSTHROUGH
#          -COMPAT-JOBS
# ------------------------------------------------------------
# ✔ ranking由来サマリーjobの互換入口
# ✔ resolve_ranking_summary_runner() の自己再帰を防止
# ✔ scheduler から渡される now をそのまま runner へ伝搬
# ✔ job_ranking_1m / 3m / 5m を提供
# ✔ 失敗時は空DataFrameで安全に返す
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _safe_log_error(msg: str, *args, exc: Exception | None = None) -> None:
    try:
        if exc is None:
            logger.error(msg, *args, exc_info=False)
        else:
            logger.error(
                msg + " err=%s: %s",
                *args,
                type(exc).__name__,
                str(exc)[:300],
                exc_info=False,
            )
    except Exception:
        pass


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame()


def _normalize_result(result: Any) -> pd.DataFrame:
    try:
        if isinstance(result, pd.DataFrame):
            return result.copy()

        if isinstance(result, tuple) and len(result) >= 1:
            first = result[0]
            if isinstance(first, pd.DataFrame):
                return first.copy()

        if isinstance(result, dict):
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
                    return val.copy()

        return _empty_df()
    except Exception:
        return _empty_df()


def _is_self_recursive_runner(runner: Any) -> bool:
    try:
        if runner is job_ranking_summary:
            return True
    except Exception:
        pass

    try:
        runner_mod = getattr(runner, "__module__", "")
        runner_name = getattr(runner, "__name__", "")
        if runner_mod == __name__ and runner_name == "job_ranking_summary":
            return True
    except Exception:
        pass

    return False


def job_ranking_summary(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    try:
        from .dependencies import resolve_ranking_summary_runner

        interval = int(interval)
        runner = resolve_ranking_summary_runner()

        if not callable(runner):
            logger.warning(
                "[summary.ranking_summary] runner unavailable interval=%s display=%s now=%s",
                interval,
                display,
                now,
            )
            return _empty_df()

        if _is_self_recursive_runner(runner):
            logger.warning(
                "[summary.ranking_summary] self-recursion detected interval=%s runner=%s.%s -> skip",
                interval,
                getattr(runner, "__module__", ""),
                getattr(runner, "__name__", str(runner)),
            )
            return _empty_df()

        runner_mod = getattr(runner, "__module__", "")
        runner_name = getattr(runner, "__name__", "")

        logger.info(
            "[summary.ranking_summary] start interval=%s runner=%s.%s display=%s now=%s extra_keys=%s",
            interval,
            runner_mod,
            runner_name,
            display,
            now,
            sorted(list(kwargs.keys())),
        )

        try:
            result = runner(interval=interval, display=display, now=now, **kwargs)
        except TypeError:
            result = runner(interval=interval, display=display, **kwargs)

        df = _normalize_result(result)

        logger.info(
            "[summary.ranking_summary] finished interval=%s rows=%s runner=%s.%s now=%s",
            interval,
            len(df),
            runner_mod,
            runner_name,
            now,
        )
        return df

    except Exception as e:
        _safe_log_error(
            "[summary.ranking_summary] job_ranking_summary failed interval=%s now=%s",
            interval,
            now,
            exc=e,
        )
        return _empty_df()


def job_ranking_1m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    return job_ranking_summary(interval=1, display=display, now=now, **kwargs)


def job_ranking_3m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    return job_ranking_summary(interval=3, display=display, now=now, **kwargs)


def job_ranking_5m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    return job_ranking_summary(interval=5, display=display, now=now, **kwargs)


def run_ranking_summary_job_compat(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    return job_ranking_summary(interval=interval, display=display, now=now, **kwargs)


__all__ = [
    "job_ranking_summary",
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "run_ranking_summary_job_compat",
]