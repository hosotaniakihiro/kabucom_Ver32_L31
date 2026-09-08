# ============================================================
# File   : core/yahoo_tasks.py
# Version: PRODUCTION-STABLE-REV5.0-YAHOO-STUCK-WORKER-DETACH
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完タスク登録と実行ラッパ。
#
# REV5.0 修正:
#   - worker が timeout を大きく超えても thread_alive=True のまま残ると、
#     毎分 tick が永久に SKIP_RUNNING になる問題を防ぐ。
#   - Python thread は kill できないため、timeout + grace 超過後は古い worker を
#     detached として無視し、短い cooldown 後に次 worker を許可する。
#   - detached された古い worker が finally で新しい worker の状態を消さないよう、
#     worker_id で所有権を確認してから状態更新する。
#   - 完全な同時多重は避けるため、YAHOO_COMPLEMENT_STUCK_COOLDOWN_SEC を設ける。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Any, Iterable

import schedule

from trading.yahoo.scheduler.complement_scheduler import yahoo_minutely_complement_job

logger = logging.getLogger(__name__)

try:
    from trading.runtime_persistence.heartbeat_watchdog import heartbeat
except Exception:
    def heartbeat(*args, **kwargs):
        return None


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
YAHOO_TASK_STUCK_GRACE_SEC = _env_int("YAHOO_COMPLEMENT_STUCK_GRACE_SEC", 60, min_value=0, max_value=600)
YAHOO_TASK_STUCK_COOLDOWN_SEC = _env_int("YAHOO_COMPLEMENT_STUCK_COOLDOWN_SEC", 90, min_value=10, max_value=900)
YAHOO_TASK_JOIN_WARN_SEC = 1.0
_TAG_YAHOO_COMPLEMENT = "yahoo_complement_database_owner"


_yahoo_lock = threading.Lock()
_yahoo_running = False
_yahoo_started_at_epoch: float = 0.0
_yahoo_last_finished_at_epoch: float = 0.0
_yahoo_last_duration_sec: float = 0.0
_yahoo_last_started_at_text: str = ""
_yahoo_last_finished_at_text: str = ""
_yahoo_last_error: str = ""
_yahoo_worker_thread: threading.Thread | None = None
_yahoo_worker_id: int = 0
_yahoo_active_worker_id: int = 0
_yahoo_detached_worker_ids: set[int] = set()
_yahoo_stuck_cooldown_until_epoch: float = 0.0
_yahoo_run_count = 0
_yahoo_skip_count = 0
_yahoo_stuck_detach_count = 0

# ------------------------------------------------------------
# generation guard (2026-09-03)
# ------------------------------------------------------------
# detach された旧 worker はスレッドとしては走り続ける。
# 「自分はもう旧世代か」を worker 自身が判定できるようにして、
# summary DB 書き込みなどの重い副作用の手前で降りられるようにする。
_yahoo_worker_local = threading.local()


def current_yahoo_worker_id() -> int:
    """このスレッドが担当している Yahoo worker の世代番号 (worker 外なら 0)。"""
    return int(getattr(_yahoo_worker_local, "worker_id", 0) or 0)


def yahoo_worker_should_stop(label: str = "") -> bool:
    """このスレッドの Yahoo worker が既に旧世代 (detached) かどうか。

    True を返したら、呼び出し側は DB 書き込み等の副作用を行わずに降りること。
    Yahoo worker スレッドの外 (worker_id=0) では常に False を返すので、
    単体実行・起動時 1 回実行の経路には影響しない。

    YAHOO_TASK_GENERATION_GUARD=0 で無効化できる。
    """
    v = os.environ.get("YAHOO_TASK_GENERATION_GUARD")
    if v is not None and str(v).strip().lower() in {"0", "false", "no", "off"}:
        return False

    wid = current_yahoo_worker_id()
    if wid <= 0:
        return False

    with _yahoo_lock:
        detached = wid in _yahoo_detached_worker_ids
        superseded = (_yahoo_active_worker_id != 0 and wid != _yahoo_active_worker_id)
        active = _yahoo_active_worker_id

    if not (detached or superseded):
        return False

    logger.warning(
        "[YAHOO TASK][GENERATION GUARD] 旧世代 worker なので中断する "
        "label=%s worker_id=%s active_worker_id=%s detached=%s superseded=%s",
        label or "-", wid, active, detached, superseded,
    )
    return True


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
    return (now_ts - _yahoo_started_at_epoch) >= YAHOO_TASK_TIMEOUT_SEC


def _reset_stale_running_if_needed(now_ts: float) -> None:
    """
    timeout超過workerの扱い。

    Python threadは安全にkillできないため、timeout直後は従来どおりskipする。
    ただし timeout+grace を超えても thread_alive=True のままなら、古いworkerを
    detached として状態管理から外す。これで毎分tickが永久skipになるのを防ぐ。
    """
    global _yahoo_running
    global _yahoo_started_at_epoch
    global _yahoo_worker_thread
    global _yahoo_active_worker_id
    global _yahoo_stuck_cooldown_until_epoch
    global _yahoo_stuck_detach_count

    if not _is_stale_running(now_ts):
        return

    elapsed = now_ts - _yahoo_started_at_epoch
    alive = _is_worker_alive()
    worker_id = _yahoo_active_worker_id

    logger.warning(
        "[YAHOO TASK] stale running detected elapsed=%.1fs timeout=%.1fs grace=%ss thread_alive=%s worker_id=%s",
        elapsed,
        float(YAHOO_TASK_TIMEOUT_SEC),
        YAHOO_TASK_STUCK_GRACE_SEC,
        alive,
        worker_id,
    )
    heartbeat(
        "yahoo_complement_task",
        status="STALE_ALIVE" if alive else "STALE_DEAD_RESET",
        detail={"elapsed_sec": elapsed, "timeout_sec": YAHOO_TASK_TIMEOUT_SEC, "grace_sec": YAHOO_TASK_STUCK_GRACE_SEC, "thread_alive": alive, "worker_id": worker_id},
    )

    if alive and elapsed < (YAHOO_TASK_TIMEOUT_SEC + YAHOO_TASK_STUCK_GRACE_SEC):
        return

    if alive:
        _yahoo_detached_worker_ids.add(worker_id)
        _yahoo_stuck_detach_count += 1
        _yahoo_stuck_cooldown_until_epoch = now_ts + YAHOO_TASK_STUCK_COOLDOWN_SEC
        logger.warning(
            "[YAHOO TASK] stale worker detached elapsed=%.1fs worker_id=%s detach_count=%s cooldown_until=%s",
            elapsed,
            worker_id,
            _yahoo_stuck_detach_count,
            dt.datetime.fromtimestamp(_yahoo_stuck_cooldown_until_epoch).strftime("%Y-%m-%d %H:%M:%S"),
        )
        heartbeat(
            "yahoo_complement_task",
            status="STALE_DETACHED",
            detail={"elapsed_sec": elapsed, "worker_id": worker_id, "cooldown_sec": YAHOO_TASK_STUCK_COOLDOWN_SEC, "detach_count": _yahoo_stuck_detach_count},
        )

    _yahoo_running = False
    _yahoo_started_at_epoch = 0.0
    _yahoo_worker_thread = None
    _yahoo_active_worker_id = 0


def _should_register_yahoo_here() -> bool:
    try:
        from data_collectors.split_mode import (
            should_run_yahoo_complement_in_this_process,
            yahoo_complement_owner,
            is_data_collector_process,
        )
        ok = bool(should_run_yahoo_complement_in_this_process())
        logger.info("[YAHOO TASK] owner check ok=%s owner=%s is_data_collector=%s", ok, yahoo_complement_owner(), is_data_collector_process())
        return ok
    except Exception:
        logger.warning("[YAHOO TASK] owner check failed -> allow registration", exc_info=True)
        return True


def _run_yahoo_job_body(started_at: dt.datetime, worker_id: int) -> None:
    global _yahoo_running
    global _yahoo_started_at_epoch
    global _yahoo_last_finished_at_epoch
    global _yahoo_last_duration_sec
    global _yahoo_last_started_at_text
    global _yahoo_last_finished_at_text
    global _yahoo_last_error
    global _yahoo_worker_thread
    global _yahoo_run_count
    global _yahoo_active_worker_id

    _yahoo_last_error = ""
    _yahoo_last_started_at_text = started_at.strftime("%Y-%m-%d %H:%M:%S")

    # このスレッドの世代番号を残す。detach された後に
    # yahoo_worker_should_stop() が True を返せるようにするため。
    _yahoo_worker_local.worker_id = int(worker_id)

    try:
        logger.info("[YAHOO TASK] worker start at=%s worker_id=%s purpose=download_1m_calc_technicals_save_summary_db intervals=1,3,5", _yahoo_last_started_at_text, worker_id)
        heartbeat("yahoo_complement_task", status="RUNNING", detail={"started_at": _yahoo_last_started_at_text, "intervals": [1, 3, 5], "worker_id": worker_id})

        yahoo_minutely_complement_job()
        _yahoo_run_count += 1

        logger.info("[YAHOO TASK] worker done at=%s worker_id=%s", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), worker_id)
        heartbeat("yahoo_complement_task", status="DONE", detail={"started_at": _yahoo_last_started_at_text, "run_count": _yahoo_run_count, "worker_id": worker_id})

    except Exception as e:
        _yahoo_last_error = repr(e)
        heartbeat("yahoo_complement_task", status="ERROR", detail={"started_at": _yahoo_last_started_at_text, "error": _yahoo_last_error, "worker_id": worker_id})
        logger.exception("[YAHOO TASK] worker failed worker_id=%s", worker_id)
    finally:
        finished = time.time()
        duration = max(finished - _yahoo_started_at_epoch, 0.0) if _yahoo_started_at_epoch > 0 else 0.0
        finished_text = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with _yahoo_lock:
            is_detached = worker_id in _yahoo_detached_worker_ids
            is_owner = worker_id == _yahoo_active_worker_id
            if is_owner:
                _yahoo_last_finished_at_epoch = finished
                _yahoo_last_finished_at_text = finished_text
                _yahoo_last_duration_sec = duration
                _yahoo_running = False
                _yahoo_started_at_epoch = 0.0
                _yahoo_worker_thread = None
                _yahoo_active_worker_id = 0
                logger.info("[YAHOO TASK] worker finalized duration=%.3fs last_finished=%s run_count=%s skip_count=%s last_error=%s worker_id=%s", _yahoo_last_duration_sec, _yahoo_last_finished_at_text, _yahoo_run_count, _yahoo_skip_count, _yahoo_last_error or "", worker_id)
            else:
                logger.warning("[YAHOO TASK] detached/old worker finalized ignored worker_id=%s active_worker_id=%s detached=%s", worker_id, _yahoo_active_worker_id, is_detached)
                _yahoo_detached_worker_ids.discard(worker_id)


def _start_yahoo_worker_core(now: dt.datetime) -> bool:
    global _yahoo_running
    global _yahoo_started_at_epoch
    global _yahoo_worker_thread
    global _yahoo_skip_count
    global _yahoo_worker_id
    global _yahoo_active_worker_id

    with _yahoo_lock:
        now_ts = time.time()
        _reset_stale_running_if_needed(now_ts)

        if now_ts < _yahoo_stuck_cooldown_until_epoch:
            remain = _yahoo_stuck_cooldown_until_epoch - now_ts
            _yahoo_skip_count += 1
            logger.warning("[YAHOO TASK] skipped because stale-worker cooldown remain=%.1fs skip_count=%s", remain, _yahoo_skip_count)
            heartbeat("yahoo_complement_task", status="SKIP_STUCK_COOLDOWN", detail={"remain_sec": remain, "skip_count": _yahoo_skip_count})
            return False

        if _yahoo_running or _is_worker_alive():
            elapsed = now_ts - _yahoo_started_at_epoch if _yahoo_started_at_epoch > 0 else -1.0
            _yahoo_skip_count += 1
            logger.warning("[YAHOO TASK] skipped because previous worker still running elapsed=%.1fs skip_count=%s worker_id=%s", elapsed, _yahoo_skip_count, _yahoo_active_worker_id)
            heartbeat("yahoo_complement_task", status="SKIP_RUNNING", detail={"elapsed_sec": elapsed, "skip_count": _yahoo_skip_count, "worker_id": _yahoo_active_worker_id})
            return False

        _yahoo_worker_id += 1
        worker_id = _yahoo_worker_id
        _yahoo_active_worker_id = worker_id
        _yahoo_running = True
        _yahoo_started_at_epoch = now_ts

        th = threading.Thread(target=_run_yahoo_job_body, args=(now, worker_id), name=f"YahooComplementWorker-{worker_id}", daemon=True)
        _yahoo_worker_thread = th
        th.start()

        logger.info("[YAHOO TASK] worker spawned at=%s thread=%s worker_id=%s cadence=%ss at_second=%s timeout=%ss grace=%ss stuck_cooldown=%ss", now.strftime("%Y-%m-%d %H:%M:%S"), th.name, worker_id, YAHOO_TASK_EVERY_SECONDS, YAHOO_TASK_AT_SECOND, YAHOO_TASK_TIMEOUT_SEC, YAHOO_TASK_STUCK_GRACE_SEC, YAHOO_TASK_STUCK_COOLDOWN_SEC)
        return True


def _yahoo_wrapper():
    now = dt.datetime.now()
    if _is_before_start_time(now):
        logger.info("⏳ Yahoo補完: %s までは実行しません（スキップ）", YAHOO_START_TIME.strftime("%H:%M"))
        return
    started = _start_yahoo_worker(now)
    if not started:
        logger.info("⏭ Yahoo補完: 前回 worker 実行中/クールダウン中のため今回の1分tickはスキップ")
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
        "worker_id": _yahoo_active_worker_id,
        "timeout_sec": YAHOO_TASK_TIMEOUT_SEC,
        "grace_sec": YAHOO_TASK_STUCK_GRACE_SEC,
        "stuck_cooldown_sec": YAHOO_TASK_STUCK_COOLDOWN_SEC,
        "stuck_cooldown_until_epoch": _yahoo_stuck_cooldown_until_epoch,
        "every_seconds": YAHOO_TASK_EVERY_SECONDS,
        "at_second": YAHOO_TASK_AT_SECOND,
        "start_time": YAHOO_START_TIME.strftime("%H:%M"),
        "run_count": _yahoo_run_count,
        "skip_count": _yahoo_skip_count,
        "stuck_detach_count": _yahoo_stuck_detach_count,
        "last_error": _yahoo_last_error,
        "tag": _TAG_YAHOO_COMPLEMENT,
    }


def register_yahoo_tasks():
    if not _should_register_yahoo_here():
        logger.warning("Yahoo補完タスク登録スキップ: this process is not Yahoo complement owner")
        return False

    try:
        schedule.clear(_TAG_YAHOO_COMPLEMENT)
    except Exception:
        pass

    if YAHOO_TASK_EVERY_SECONDS == 60:
        at_text = f":{YAHOO_TASK_AT_SECOND:02d}"
        job = schedule.every().minute.at(at_text).do(_yahoo_wrapper)
    else:
        job = schedule.every(YAHOO_TASK_EVERY_SECONDS).seconds.do(_yahoo_wrapper)

    try:
        job.tag(_TAG_YAHOO_COMPLEMENT)
    except Exception:
        pass

    logger.info("Yahoo補完タスク登録済み start=%s cadence=%ss at_second=%s nonblocking=True timeout=%ss grace=%ss stuck_cooldown=%ss tag=%s job_next=%s", YAHOO_START_TIME.strftime("%H:%M"), YAHOO_TASK_EVERY_SECONDS, YAHOO_TASK_AT_SECOND, YAHOO_TASK_TIMEOUT_SEC, YAHOO_TASK_STUCK_GRACE_SEC, YAHOO_TASK_STUCK_COOLDOWN_SEC, _TAG_YAHOO_COMPLEMENT, getattr(job, "next_run", None))
    heartbeat("yahoo_complement_task", status="REGISTERED", detail={"start_time": YAHOO_START_TIME.strftime("%H:%M"), "every_seconds": YAHOO_TASK_EVERY_SECONDS, "at_second": YAHOO_TASK_AT_SECOND, "timeout_sec": YAHOO_TASK_TIMEOUT_SEC, "grace_sec": YAHOO_TASK_STUCK_GRACE_SEC, "stuck_cooldown_sec": YAHOO_TASK_STUCK_COOLDOWN_SEC, "next_run": str(getattr(job, "next_run", None))})
    return True


__all__ = ["yahoo_minutely_complement_job", "register_yahoo_tasks", "get_yahoo_task_status"]


# ============================================================
# 旧 core/startup/intraday_load_guard_patch._patch_yahoo_tasks を本文化。
# 日中の Yahoo 補完 worker が 1 分ごとに重複起動しないよう、
# 最短起動間隔 (YAHOO_COMPLEMENT_MIN_START_GAP_SEC) でスロットルする。
# 適用スコープは旧 _safe_install の allowed 集合
# (DB|TRADING|HELPER = ほぼ全 context) を _ilg_scope_enabled() で再現。
# ============================================================

_ILG_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_ILG_CONTEXTS = {
    "main_database", "db_prepare", "push_receiver", "summary_database",
    "ranking_collector", "yahoo_complement", "main", "helper",
}


def _ilg_argv_context() -> str:
    """core/startup/runtime_env/installer.py の _argv_context() と同一。"""
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text:
            return "main_database"
        if text.endswith("main.py") or "/main.py" in text:
            return "main"
        if "db_prepare_runner.py" in text:
            return "db_prepare"
        if "push_receiver_runner.py" in text:
            return "push_receiver"
        if "summary_database_runner.py" in text:
            return "summary_database"
        if "ranking_collector_runner.py" in text:
            return "ranking_collector"
        if "yahoo_complement_runner.py" in text:
            return "yahoo_complement"
        if "data_collectors_runner.py" in text:
            return "main_database"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _ilg_scope_enabled() -> bool:
    if str(os.environ.get("DISABLE_INTRADAY_LOAD_GUARD_PATCH", "")).strip() == "1":
        return False
    if _ilg_env_bool("DISABLE_INTRADAY_LOAD_GUARD_PATCH", False):
        return False
    return _ilg_argv_context() in _ILG_CONTEXTS


def _ilg_env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _ILG_TRUE:
            return True
        if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _ilg_env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(float(str(os.getenv(name, str(default))).replace(",", "")))
    except Exception:
        v = int(default)
    if min_value is not None:
        v = max(v, min_value)
    if max_value is not None:
        v = min(v, max_value)
    return int(v)


def _ilg_norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    return s


def _ilg_dedupe_limit(items: Iterable[Any], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = _ilg_norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


_ilg_last_attempt_at = 0.0
_ilg_skip_counter = 0


def _start_yahoo_worker(now: dt.datetime) -> bool:
    global _ilg_last_attempt_at, _ilg_skip_counter
    if not _ilg_scope_enabled():
        return _start_yahoo_worker_core(now)
    min_gap = _ilg_env_int("YAHOO_COMPLEMENT_MIN_START_GAP_SEC", 240, min_value=0, max_value=3600)
    now_ts = time.time()
    if min_gap > 0 and _ilg_last_attempt_at > 0 and (now_ts - _ilg_last_attempt_at) < min_gap:
        _ilg_skip_counter += 1
        every = _ilg_env_int("YAHOO_COMPLEMENT_SKIP_LOG_EVERY", 5, min_value=1, max_value=100)
        if _ilg_skip_counter == 1 or _ilg_skip_counter % every == 0:
            logger.warning(
                "[INTRADAY LOAD GUARD] Yahoo complement throttled elapsed=%.1fs min_gap=%ss skip_count=%s",
                now_ts - _ilg_last_attempt_at,
                min_gap,
                _ilg_skip_counter,
            )
        return False
    started = bool(_start_yahoo_worker_core(now))
    if started:
        _ilg_last_attempt_at = now_ts
        _ilg_skip_counter = 0
    return started
