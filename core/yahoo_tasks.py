# ============================================================
# File   : core/yahoo_tasks.py
# Version: PRODUCTION-STABLE-REV3.0-YAHOO-TASKS-DATABASE-OWNER-GUARD
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完タスク登録と実行ラッパ。
#
# 【目的】
#   - schedule 本体を Yahoo 補完の長時間処理でブロックしない
#   - previous still running による毎分スキップ連鎖を防ぐ
#   - 9:20 より前は実行しない
#   - 二重起動を防ぐ
#   - stale 実行状態を一定時間で解放する
#   - main_database.py 側だけでYahoo補完の取得・DB保存を動かせる
#
# REV3:
#   ✔ data_collectors.split_mode の Yahoo owner 設定を尊重
#   ✔ 既定 owner=database のため main.py では Yahoo保存ジョブを登録しない
#   ✔ main_database.py 側の yahoo_complement_runner.py では登録する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import schedule

from trading.yahoo.scheduler.complement_scheduler import yahoo_minutely_complement_job

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# config
# ------------------------------------------------------------

YAHOO_START_TIME = dt.time(9, 20)
YAHOO_TASK_TIMEOUT_SEC = 90
YAHOO_TASK_JOIN_WARN_SEC = 1.0
_TAG_YAHOO_COMPLEMENT = "yahoo_complement_database_owner"

# ------------------------------------------------------------
# runtime state
# ------------------------------------------------------------

_yahoo_lock = threading.Lock()
_yahoo_running = False
_yahoo_started_at_epoch: float = 0.0
_yahoo_last_finished_at_epoch: float = 0.0
_yahoo_last_duration_sec: float = 0.0
_yahoo_worker_thread: threading.Thread | None = None


def _is_before_start_time(now: dt.datetime) -> bool:
    return now.time() < YAHOO_START_TIME


def _is_stale_running(now_ts: float) -> bool:
    global _yahoo_running
    global _yahoo_started_at_epoch

    if not _yahoo_running:
        return False

    if _yahoo_started_at_epoch <= 0:
        return False

    elapsed = now_ts - _yahoo_started_at_epoch
    return elapsed >= YAHOO_TASK_TIMEOUT_SEC


def _reset_stale_running_if_needed(now_ts: float) -> None:
    global _yahoo_running
    global _yahoo_started_at_epoch
    global _yahoo_worker_thread

    if not _is_stale_running(now_ts):
        return

    elapsed = now_ts - _yahoo_started_at_epoch

    logger.warning(
        "[YAHOO TASK] stale running detected elapsed=%.1fs timeout=%.1fs -> reset running flag",
        elapsed,
        float(YAHOO_TASK_TIMEOUT_SEC),
    )

    # 注意:
    # 実スレッド自体は kill できないため、running フラグだけ解放する。
    # 本体側も可能なら短時間で戻るようにすべき。
    _yahoo_running = False
    _yahoo_started_at_epoch = 0.0
    _yahoo_worker_thread = None


def _should_register_yahoo_here() -> bool:
    try:
        from data_collectors.split_mode import (
            should_run_yahoo_complement_in_this_process,
            yahoo_complement_owner,
            is_data_collector_process,
        )

        ok = bool(should_run_yahoo_complement_in_this_process())
        logger.info(
            "[YAHOO TASK] owner check ok=%s owner=%s is_data_collector=%s",
            ok,
            yahoo_complement_owner(),
            is_data_collector_process(),
        )
        return ok

    except Exception:
        # split_modeが無い古い環境では従来通り登録する。
        logger.warning("[YAHOO TASK] owner check failed -> allow registration", exc_info=True)
        return True


def _run_yahoo_job_body(started_at: dt.datetime) -> None:
    global _yahoo_running
    global _yahoo_started_at_epoch
    global _yahoo_last_finished_at_epoch
    global _yahoo_last_duration_sec
    global _yahoo_worker_thread

    try:
        logger.info("[YAHOO TASK] worker start at=%s", started_at)

        yahoo_minutely_complement_job()

        logger.info("[YAHOO TASK] worker done at=%s", dt.datetime.now())

    except Exception:
        logger.exception("[YAHOO TASK] worker failed")
    finally:
        finished = time.time()
        _yahoo_last_finished_at_epoch = finished

        if _yahoo_started_at_epoch > 0:
            _yahoo_last_duration_sec = max(finished - _yahoo_started_at_epoch, 0.0)
        else:
            _yahoo_last_duration_sec = 0.0

        _yahoo_running = False
        _yahoo_started_at_epoch = 0.0
        _yahoo_worker_thread = None

        logger.info(
            "[YAHOO TASK] worker finalized duration=%.3fs",
            _yahoo_last_duration_sec,
        )


def _start_yahoo_worker(now: dt.datetime) -> bool:
    global _yahoo_running
    global _yahoo_started_at_epoch
    global _yahoo_worker_thread

    with _yahoo_lock:
        now_ts = time.time()

        _reset_stale_running_if_needed(now_ts)

        if _yahoo_running:
            elapsed = now_ts - _yahoo_started_at_epoch if _yahoo_started_at_epoch > 0 else -1.0
            logger.warning(
                "[YAHOO TASK] skipped because worker still running elapsed=%.1fs",
                elapsed,
            )
            return False

        _yahoo_running = True
        _yahoo_started_at_epoch = now_ts

        th = threading.Thread(
            target=_run_yahoo_job_body,
            args=(now,),
            name="YahooComplementWorker",
            daemon=True,
        )
        _yahoo_worker_thread = th
        th.start()

        logger.info(
            "[YAHOO TASK] worker spawned at=%s thread=%s",
            now,
            th.name,
        )
        return True


def _yahoo_wrapper():
    """
    9:20 までは Yahoo 補完を実行しない。
    schedule スレッドはブロックせず、実処理は別スレッドへ委譲する。
    """
    now = dt.datetime.now()

    if _is_before_start_time(now):
        logger.info("⏳ Yahoo補完: 9:20 までは実行しません（スキップ）")
        return

    started = _start_yahoo_worker(now)

    if not started:
        logger.info("⏭ Yahoo補完: 前回 worker 実行中のためスキップ")
        return


def get_yahoo_task_status() -> dict:
    now_ts = time.time()

    running = _yahoo_running
    started_at = _yahoo_started_at_epoch
    elapsed = (now_ts - started_at) if running and started_at > 0 else 0.0

    thread_name = None
    try:
        if _yahoo_worker_thread is not None:
            thread_name = _yahoo_worker_thread.name
    except Exception:
        thread_name = None

    return {
        "running": running,
        "started_at_epoch": started_at,
        "last_finished_at_epoch": _yahoo_last_finished_at_epoch,
        "last_duration_sec": _yahoo_last_duration_sec,
        "elapsed_sec": elapsed,
        "thread_name": thread_name,
        "timeout_sec": YAHOO_TASK_TIMEOUT_SEC,
        "start_time": str(YAHOO_START_TIME),
        "tag": _TAG_YAHOO_COMPLEMENT,
    }


def register_yahoo_tasks():
    if not _should_register_yahoo_here():
        logger.warning(
            "Yahoo補完タスク登録スキップ: this process is not Yahoo complement owner"
        )
        return False

    try:
        schedule.clear(_TAG_YAHOO_COMPLEMENT)
    except Exception:
        pass

    # 衝突しない毎分 :10 に設定
    job = schedule.every().minute.at(":10").do(_yahoo_wrapper)
    try:
        job.tag(_TAG_YAHOO_COMPLEMENT)
    except Exception:
        pass

    logger.info(
        "Yahoo補完タスク登録済み（%s以降実行, nonblocking, timeout=%ss, tag=%s）",
        YAHOO_START_TIME.strftime("%H:%M"),
        YAHOO_TASK_TIMEOUT_SEC,
        _TAG_YAHOO_COMPLEMENT,
    )
    return True


__all__ = ["yahoo_minutely_complement_job", "register_yahoo_tasks", "get_yahoo_task_status"]
