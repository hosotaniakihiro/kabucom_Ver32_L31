# ============================================================
# File   : scheduler_jobs/summary/register_ranking_summary_jobs.py
# Ver    : PRODUCTION-STABLE-REV1.0-REGISTER-RANKING-SUMMARY-JOBS
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリー専用ジョブを APScheduler に登録する
#
# 【重要方針】
#   - PUSH由来 summary とは完全分離
#   - stock_summary_* は読まない・書かない
#   - ranking_summary_1min / 3min / 5min を定時作成
#   - Yahoo 1分足 close 補完をランキング由来サマリーでも利用
#
# 【登録されるジョブ】
#   - ranking_summary_all
#
# 【実行タイミング】
#   - 毎分 10秒
#   - 内部で現在時刻により 1min / 3min / 5min を分岐
#
# 【実行例】
#   09:01:10 -> 1min
#   09:03:10 -> 1min + 3min
#   09:05:10 -> 1min + 5min
#   09:15:10 -> 1min + 3min + 5min
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

JOB_ID_RANKING_SUMMARY_ALL = "ranking_summary_all"

DEFAULT_LOOKBACK_MINUTES = 240
DEFAULT_JOB_SECOND = 10


# ============================================================
# Scheduler helpers
# ============================================================

def _has_job(scheduler: Any, job_id: str) -> bool:
    """
    scheduler に指定job_idが登録済みか確認する。
    """
    try:
        return scheduler.get_job(job_id) is not None
    except Exception:
        return False


def _remove_job_if_exists(scheduler: Any, job_id: str) -> None:
    """
    replace_existing 非対応の scheduler 実装にも備えて明示削除する。
    """
    try:
        job = scheduler.get_job(job_id)
        if job is not None:
            scheduler.remove_job(job_id)
            logger.info("[RANKING SUMMARY REGISTER] removed old job id=%s", job_id)
    except Exception:
        logger.debug(
            "[RANKING SUMMARY REGISTER] remove old job skipped id=%s",
            job_id,
            exc_info=True,
        )


def _safe_add_cron_job(
    scheduler: Any,
    func,
    *,
    job_id: str,
    second: int,
    kwargs: Optional[dict[str, Any]] = None,
    replace_existing: bool = True,
) -> bool:
    """
    APScheduler.add_job を安全に呼ぶ。
    """
    kwargs = kwargs or {}

    if scheduler is None:
        logger.warning("[RANKING SUMMARY REGISTER] scheduler is None")
        return False

    try:
        if replace_existing:
            _remove_job_if_exists(scheduler, job_id)

        scheduler.add_job(
            func,
            "cron",
            second=int(second),
            id=job_id,
            replace_existing=True,
            kwargs=kwargs,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )

        logger.info(
            "[RANKING SUMMARY REGISTER] registered job id=%s second=%s kwargs=%s",
            job_id,
            second,
            kwargs,
        )
        return True

    except TypeError:
        # 古い/薄いschedulerラッパーで max_instances 等を受けない場合
        try:
            scheduler.add_job(
                func,
                "cron",
                second=int(second),
                id=job_id,
                replace_existing=True,
                kwargs=kwargs,
            )

            logger.info(
                "[RANKING SUMMARY REGISTER] registered job id=%s second=%s kwargs=%s "
                "(compat mode)",
                job_id,
                second,
                kwargs,
            )
            return True

        except Exception:
            logger.exception(
                "[RANKING SUMMARY REGISTER] add_job failed compat id=%s",
                job_id,
            )
            return False

    except Exception:
        logger.exception(
            "[RANKING SUMMARY REGISTER] add_job failed id=%s",
            job_id,
        )
        return False


# ============================================================
# Public register function
# ============================================================

def register_ranking_summary_jobs(
    scheduler: Any,
    *,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    second: int = DEFAULT_JOB_SECOND,
    replace_existing: bool = True,
    run_once_on_startup: bool = False,
) -> bool:
    """
    ランキング由来サマリー定時ジョブを登録する。

    Parameters
    ----------
    scheduler:
        APScheduler または互換 scheduler。

    lookback_minutes:
        ranking_snapshot_1min / Yahoo 1min を何分ぶん読むか。

    use_yahoo_fill:
        True の場合、ランキング価格系列の欠損を Yahoo close で補完する。

    persist:
        True の場合、ranking_summary_1min / 3min / 5min に保存する。

    display:
        True の場合、RANKING SUMMARY TOP10 をログ表示する。

    second:
        毎分何秒に実行するか。
        ranking保存・Yahoo補完の直後に回すため、10秒を推奨。

    run_once_on_startup:
        True の場合、登録直後に force=True で1回実行する。
    """
    if scheduler is None:
        logger.warning("[RANKING SUMMARY REGISTER] scheduler is None")
        return False

    try:
        from scheduler_jobs.summary.ranking_summary_jobs import (
            job_ranking_summary_all,
        )
    except Exception:
        logger.exception(
            "[RANKING SUMMARY REGISTER] import failed "
            "scheduler_jobs.summary.ranking_summary_jobs.job_ranking_summary_all"
        )
        return False

    kwargs = {
        "force": False,
        "lookback_minutes": int(lookback_minutes),
        "use_yahoo_fill": bool(use_yahoo_fill),
        "persist": bool(persist),
        "display": bool(display),
    }

    ok = _safe_add_cron_job(
        scheduler,
        job_ranking_summary_all,
        job_id=JOB_ID_RANKING_SUMMARY_ALL,
        second=int(second),
        kwargs=kwargs,
        replace_existing=replace_existing,
    )

    if not ok:
        return False

    if run_once_on_startup:
        try:
            logger.info(
                "[RANKING SUMMARY REGISTER] startup force run start "
                "lookback=%s yahoo_fill=%s persist=%s display=%s",
                lookback_minutes,
                use_yahoo_fill,
                persist,
                display,
            )

            job_ranking_summary_all(
                force=True,
                lookback_minutes=int(lookback_minutes),
                use_yahoo_fill=bool(use_yahoo_fill),
                persist=bool(persist),
                display=bool(display),
            )

            logger.info("[RANKING SUMMARY REGISTER] startup force run done")

        except Exception:
            logger.exception(
                "[RANKING SUMMARY REGISTER] startup force run failed"
            )

    return True


# ============================================================
# Compatibility aliases
# ============================================================

def register_jobs(scheduler: Any, **kwargs) -> bool:
    """
    既存 bootstrap が register_jobs を探す場合の互換入口。
    """
    return register_ranking_summary_jobs(scheduler, **kwargs)


def register_ranking_jobs(scheduler: Any, **kwargs) -> bool:
    """
    互換入口。
    """
    return register_ranking_summary_jobs(scheduler, **kwargs)


__all__ = [
    "JOB_ID_RANKING_SUMMARY_ALL",
    "register_ranking_summary_jobs",
    "register_jobs",
    "register_ranking_jobs",
]