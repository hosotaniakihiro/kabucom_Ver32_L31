# ============================================================
# File   : core/yahoo_tasks.py
# Version: PRODUCTION-STABLE-REV4.0-YAHOO-MINUTELY-DB-SAVE-GUARD
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完タスク登録と実行ラッパ。
#
# 【目的】
#   - main_database.py 側だけで Yahoo 1分足取得・テクニカル計算・summary DB保存を毎分実行する
#   - schedule 本体を Yahoo 補完の長時間処理でブロックしない
#   - 二重起動/多重DB書き込みによる database is locked を防ぐ
#   - 前回worker実行中なら次の1分tickは安全にスキップする
#   - stale判定時も生存中スレッドはkillできないため、重複起動せず診断ログだけ出す
#   - main.py 側では Yahoo保存ジョブを登録しない
#
# 【処理内容】
#   yahoo_minutely_complement_job()
#     -> trading.yahoo.scheduler.complement_scheduler.yahoo_minutely_complement_job()
#     -> trading.yahoo.complement.download_flow.run_periodic_yahoo_complement()
#     -> Yahoo 1分足差分DL
#     -> yahoo_1min DB保存
#     -> 1m/3m/5m summary生成
#     -> ma/rsi/macd/slope/score等のテクニカル計算
#     -> summaryYYYYMMDD.db の stock_summary_1min/3min/5min へUPSERT
#
# 【環境変数】
#   YAHOO_COMPLEMENT_START_HHMM       既定 09:20
#   YAHOO_COMPLEMENT_EVERY_SECONDS    既定 60
#   YAHOO_COMPLEMENT_AT_SECOND        既定 10
#   YAHOO_COMPLEMENT_TIMEOUT_SEC      既定 300
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time

import schedule

from trading.yahoo.scheduler.complement_scheduler import yahoo_minutely_complement_job

logger = logging.getLogger(__name__)

try:
    from trading.runtime_persistence.heartbeat_watchdog import heartbeat
except Exception:  # heartbeat が壊れても Yahoo 補完は止めない
    def heartbeat(*args, **kwargs):
        return None


# ------------------------------------------------------------
# config
# ------------------------------------------------------------

def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(str(os.environ.get(name, str(default))).strip())
        if min_value is not None:
            v = max(v, min_value)
        if max_value is not None:
            v = min(v, max_value)
        return v
    except Exception:
        return default


def _env_time_hhmm(name: str, default_hhmm: str) -> dt.time:
    raw = str(os.environ.get(name, default_hhmm)).strip()
    try:
        hh, mm = raw.split(":", 1)
        return dt.time(int(hh), int(mm))
    except Exception:
        logger.warning("[YAHOO TASK] invalid %s=%r -> fallback %s", name, raw, default_hhmm)
        hh, mm = default_hhmm.split(":", 1)
        return dt.time(int(hh), int(mm))


YAHOO_START_TIME = _env_time_hhmm("YAHOO_COMPLEMENT_START_HHMM", "09:20")
YAHOO_TASK_EVERY_SECONDS = _env_int("YAHOO_COMPLEMENT_EVERY_SECONDS", 60, min_value=10, max_value=3600)
YAHOO_TASK_AT_SECOND = _env_int("YAHOO_COMPLEMENT_AT_SECOND", 10, min_value=0, max_value=59)
YAHOO_TASK_TIMEOUT_SEC = _env_int("YAHOO_COMPLEMENT_TIMEOUT_SEC", 300, min_value=60, max_value=1800)
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
_yahoo_last_started_at_text: str = ""
_yahoo_last_finished_at_text: str = ""
_yahoo_last_error: str = ""
_yahoo_worker_thread: threading.Thread | None = None
_yahoo_run_count = 0
_yahoo_skip_count = 0


def _is_before_start_time(now: dt.datetime) -> bool:
    return now.time() < YAHOO_START_TIME


def _is_worker_alive() -> bool:
    try:
        return _yahoo_worker_thread is not None and _yahoo_worker_thread.is_alive()
    except Exception:
        return False


def _is_stale_running(now_ts: float) -> bool:
    if not _yahoo_running:
        return False
    if _yahoo_started_at_epoch <= 0:
        return False
    elapsed = now_ts - _yahoo_started_at_epoch
    return elapsed >= YAHOO_TASK_TIMEOUT_SEC


def _reset_stale_running_if_needed(now_ts: float) -> None:
    """
    古いREVでは timeout 超過時に running flag だけを解放していた。
    しかし Python thread は kill できないため、生存中に flag だけ解放すると
    次の毎分tickで二重workerが起動し、summary DB / yahoo DB の database is locked を誘発する。

    REV4では以下に変更する。
      - thread alive: flagは解放しない。次tickも skip させる。
      - thread dead : flagだけ残っている異常状態なので解放する。
    """
    global _yahoo_running
    global _yahoo_started_at_epoch
    global _yahoo_worker_thread

    if not _is_stale_running(now_ts):
        return

    elapsed = now_ts - _yahoo_started_at_epoch
    alive = _is_worker_alive()

    logger.warning(
        "[YAHOO TASK] stale running detected elapsed=%.1fs timeout=%.1fs thread_alive=%s",
        elapsed,
        float(YAHOO_TASK_TIMEOUT_SEC),
        alive,
    )
    heartbeat(
        "yahoo_complement_task",
        status="STALE_ALIVE" if alive else "STALE_DEAD_RESET",
        detail={"elapsed_sec": elapsed, "timeout_sec": YAHOO_TASK_TIMEOUT_SEC, "thread_alive": alive},
    )

    if alive:
        # 生存中は絶対に二重起動しない。
        return

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
    global _yahoo_last_started_at_text
    global _yahoo_last_finished_at_text
    global _yahoo_last_error
    global _yahoo_worker_thread
    global _yahoo_run_count

    _yahoo_last_error = ""
    _yahoo_last_started_at_text = started_at.strftime("%Y-%m-%d %H:%M:%S")

    try:
        logger.info(
            "[YAHOO TASK] worker start at=%s purpose=download_1m_calc_technicals_save_summary_db intervals=1,3,5",
            _yahoo_last_started_at_text,
        )
        heartbeat(
            "yahoo_complement_task",
            status="RUNNING",
            detail={"started_at": _yahoo_last_started_at_text, "intervals": [1, 3, 5]},
        )

        yahoo_minutely_complement_job()
        _yahoo_run_count += 1

        logger.info("[YAHOO TASK] worker done at=%s", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        heartbeat(
            "yahoo_complement_task",
            status="DONE",
            detail={"started_at": _yahoo_last_started_at_text, "run_count": _yahoo_run_count},
        )

    except Exception as e:
        _yahoo_last_error = repr(e)
        heartbeat(
            "yahoo_complement_task",
            status="ERROR",
            detail={"started_at": _yahoo_last_started_at_text, "error": _yahoo_last_error},
        )
        logger.exception("[YAHOO TASK] worker failed")
    finally:
        finished = time.time()
        _yahoo_last_finished_at_epoch = finished
        _yahoo_last_finished_at_text = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if _yahoo_started_at_epoch > 0:
            _yahoo_last_duration_sec = max(finished - _yahoo_started_at_epoch, 0.0)
        else:
            _yahoo_last_duration_sec = 0.0

        _yahoo_running = False
        _yahoo_started_at_epoch = 0.0
        _yahoo_worker_thread = None

        logger.info(
            "[YAHOO TASK] worker finalized duration=%.3fs last_finished=%s run_count=%s skip_count=%s last_error=%s",
            _yahoo_last_duration_sec,
            _yahoo_last_finished_at_text,
            _yahoo_run_count,
            _yahoo_skip_count,
            _yahoo_last_error or "",
        )


def _start_yahoo_worker(now: dt.datetime) -> bool:
    global _yahoo_running
    global _yahoo_started_at_epoch
    global _yahoo_worker_thread
    global _yahoo_skip_count

    with _yahoo_lock:
        now_ts = time.time()

        _reset_stale_running_if_needed(now_ts)

        if _yahoo_running or _is_worker_alive():
            elapsed = now_ts - _yahoo_started_at_epoch if _yahoo_started_at_epoch > 0 else -1.0
            _yahoo_skip_count += 1
            logger.warning(
                "[YAHOO TASK] skipped because previous worker still running elapsed=%.1fs skip_count=%s",
                elapsed,
                _yahoo_skip_count,
            )
            heartbeat(
                "yahoo_complement_task",
                status="SKIP_RUNNING",
                detail={"elapsed_sec": elapsed, "skip_count": _yahoo_skip_count},
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
            "[YAHOO TASK] worker spawned at=%s thread=%s cadence=%ss at_second=%s timeout=%ss",
            now.strftime("%Y-%m-%d %H:%M:%S"),
            th.name,
            YAHOO_TASK_EVERY_SECONDS,
            YAHOO_TASK_AT_SECOND,
            YAHOO_TASK_TIMEOUT_SEC,
        )
        return True


def _yahoo_wrapper():
    """
    Yahoo補完の毎分wrapper。
    実処理は別スレッドへ委譲し、schedule loopは止めない。
    """
    now = dt.datetime.now()

    if _is_before_start_time(now):
        logger.info("⏳ Yahoo補完: %s までは実行しません（スキップ）", YAHOO_START_TIME.strftime("%H:%M"))
        return

    started = _start_yahoo_worker(now)

    if not started:
        logger.info("⏭ Yahoo補完: 前回 worker 実行中のため今回の1分tickはスキップ")
        return


def get_yahoo_task_status() -> dict:
    now_ts = time.time()

    running = _yahoo_running or _is_worker_alive()
    started_at = _yahoo_started_at_epoch
    elapsed = (now_ts - started_at) if running and started_at > 0 else 0.0

    thread_name = None
    thread_alive = False
    try:
        if _yahoo_worker_thread is not None:
            thread_name = _yahoo_worker_thread.name
            thread_alive = _yahoo_worker_thread.is_alive()
    except Exception:
        thread_name = None
        thread_alive = False

    return {
        "running": running,
        "thread_alive": thread_alive,
        "started_at_epoch": started_at,
        "last_started_at": _yahoo_last_started_at_text,
        "last_finished_at_epoch": _yahoo_last_finished_at_epoch,
        "last_finished_at": _yahoo_last_finished_at_text,
        "last_duration_sec": _yahoo_last_duration_sec,
        "elapsed_sec": elapsed,
        "thread_name": thread_name,
        "timeout_sec": YAHOO_TASK_TIMEOUT_SEC,
        "every_seconds": YAHOO_TASK_EVERY_SECONDS,
        "at_second": YAHOO_TASK_AT_SECOND,
        "start_time": YAHOO_START_TIME.strftime("%H:%M"),
        "run_count": _yahoo_run_count,
        "skip_count": _yahoo_skip_count,
        "last_error": _yahoo_last_error,
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

    # 既定: 毎分 :10 に起動。
    # 処理が1分以上かかる場合は重複起動せず SKIP_RUNNING にする。
    if YAHOO_TASK_EVERY_SECONDS == 60:
        at_text = f":{YAHOO_TASK_AT_SECOND:02d}"
        job = schedule.every().minute.at(at_text).do(_yahoo_wrapper)
    else:
        job = schedule.every(YAHOO_TASK_EVERY_SECONDS).seconds.do(_yahoo_wrapper)

    try:
        job.tag(_TAG_YAHOO_COMPLEMENT)
    except Exception:
        pass

    logger.info(
        "Yahoo補完タスク登録済み start=%s cadence=%ss at_second=%s nonblocking=True timeout=%ss tag=%s job_next=%s",
        YAHOO_START_TIME.strftime("%H:%M"),
        YAHOO_TASK_EVERY_SECONDS,
        YAHOO_TASK_AT_SECOND,
        YAHOO_TASK_TIMEOUT_SEC,
        _TAG_YAHOO_COMPLEMENT,
        getattr(job, "next_run", None),
    )
    heartbeat(
        "yahoo_complement_task",
        status="REGISTERED",
        detail={
            "start_time": YAHOO_START_TIME.strftime("%H:%M"),
            "every_seconds": YAHOO_TASK_EVERY_SECONDS,
            "at_second": YAHOO_TASK_AT_SECOND,
            "timeout_sec": YAHOO_TASK_TIMEOUT_SEC,
            "next_run": str(getattr(job, "next_run", None)),
        },
    )
    return True


__all__ = ["yahoo_minutely_complement_job", "register_yahoo_tasks", "get_yahoo_task_status"]
