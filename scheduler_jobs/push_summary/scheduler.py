# ============================================================
# File   : scheduler_jobs/push_summary/scheduler.py
# Version: Ver31_L23-PUSH-SUMMARY-SCHEDULER-SEPARATED
# ------------------------------------------------------------
# 機能:
#   - PUSH由来サマリーの定時スケジューラ登録
#   - 1分ごとの基本job登録
#   - 3分 / 5分の time-locked 実行
#   - scheduler ライブラリへの登録を PUSH系に限定
#
# 目的:
#   - PUSH由来サマリーの定時実行入口を ranking系から分離
#   - :00基準の 3分 / 5分 実行を明確化
#
# 主な関数:
#   - register_push_summary_tasks()
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import schedule

from scheduler_jobs.push_summary.jobs import (
    job_push_summary_1m,
    job_push_summary_3m,
    job_push_summary_5m,
)

logger = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now()


def _run_push_summary_tick() -> None:
    """
    毎分 :00 に呼ばれる PUSH由来サマリーの親tick
    1min は毎回実行
    3min は 00,03,06,...
    5min は 00,05,10,...
    """
    try:
        now = _now()
        minute = now.minute

        logger.info(
            "[push_summary.scheduler] tick start hhmm=%02d:%02d",
            now.hour,
            minute,
        )

        # 1min は毎分実行
        try:
            job_push_summary_1m(display=True)
        except Exception:
            logger.exception("[push_summary.scheduler] 1m job failed")

        # 3min
        if minute % 3 == 0:
            try:
                job_push_summary_3m(display=True)
            except Exception:
                logger.exception("[push_summary.scheduler] 3m job failed")

        # 5min
        if minute % 5 == 0:
            try:
                job_push_summary_5m(display=True)
            except Exception:
                logger.exception("[push_summary.scheduler] 5m job failed")

        logger.info(
            "[push_summary.scheduler] tick finished hhmm=%02d:%02d",
            now.hour,
            minute,
        )

    except Exception:
        logger.exception("[push_summary.scheduler] _run_push_summary_tick failed")


def register_push_summary_tasks() -> None:
    """
    PUSH由来サマリーの定時タスクを登録する
    毎分 :00 に親tickを実行し、その中で 1m / 3m / 5m を振り分ける
    """
    try:
        schedule.every().minute.at(":00").do(_run_push_summary_tick)
        logger.info(
            "[push_summary.scheduler] registered every minute at :00 "
            "(1m always, 3m on %3==0, 5m on %5==0)"
        )
    except Exception:
        logger.exception("[push_summary.scheduler] register_push_summary_tasks failed")