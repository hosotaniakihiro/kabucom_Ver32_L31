# ============================================================
# File   : trading/summary/persistence/lock_manager.py
# Version: Ver1.0-PRODUCTION-LOCK-MANAGER
# ------------------------------------------------------------
# 機能:
# - interval単位ロック管理
# - lock holder/waiter メタ情報管理
# - lock timeout / hold時間 診断
# - fast skip 判定
# - 低優先writer用 lock policy 調整
# ------------------------------------------------------------
# 主な責務:
# - summary保存時の同時実行制御
# - SQLite競合を抑えるための lock 制御
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_GLOBAL_LOCK_GUARD = threading.Lock()
_INTERVAL_LOCKS: dict[int, threading.RLock] = {}

_INTERVAL_LOCK_META_GUARD = threading.Lock()
_INTERVAL_LOCK_META: dict[int, dict[str, Any]] = {}

DEFAULT_LOCK_ACQUIRE_TIMEOUT_SEC = 60.0
LOCK_WAIT_WARN_SEC = 0.25
LOCK_HOLD_WARN_SEC = 5.0


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def now_wall_str() -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "unknown"


def thread_ident() -> int:
    try:
        return threading.get_ident()
    except Exception:
        return -1


def thread_name() -> str:
    try:
        return threading.current_thread().name
    except Exception:
        return "unknown"


def get_interval_lock(interval: int) -> threading.RLock:
    interval = int(interval)
    with _GLOBAL_LOCK_GUARD:
        lock = _INTERVAL_LOCKS.get(interval)
        if lock is None:
            lock = threading.RLock()
            _INTERVAL_LOCKS[interval] = lock
        return lock


def get_lock_meta(interval: int) -> dict[str, Any]:
    interval = int(interval)
    with _INTERVAL_LOCK_META_GUARD:
        return dict(_INTERVAL_LOCK_META.get(interval, {}))


def set_lock_meta(interval: int, meta: dict[str, Any]) -> None:
    interval = int(interval)
    with _INTERVAL_LOCK_META_GUARD:
        _INTERVAL_LOCK_META[interval] = dict(meta)


def clear_lock_meta(interval: int) -> None:
    interval = int(interval)
    with _INTERVAL_LOCK_META_GUARD:
        _INTERVAL_LOCK_META.pop(interval, None)


def lock_holder_snapshot(interval: int) -> dict[str, Any]:
    try:
        meta = get_lock_meta(interval)
        if not meta:
            return {}

        hold_started_mono = safe_float(meta.get("hold_started_mono"), 0.0)
        held_sec = max(0.0, time.monotonic() - hold_started_mono) if hold_started_mono > 0 else 0.0

        snap = dict(meta)
        snap["held_sec"] = held_sec
        return snap
    except Exception:
        logger.exception("[SUMMARY] lock_holder_snapshot failed interval=%s", interval)
        return {}


def is_scheduler_thread_name(name: str) -> bool:
    try:
        x = str(name or "").strip().lower()
        return ("scheduler" in x or "schedule" in x or "apscheduler" in x)
    except Exception:
        return False


def is_low_priority_interval1_writer(thread_name_value: str) -> bool:
    try:
        name = str(thread_name_value or "").strip().lower()
        if name == "mainthread":
            return True
        if is_scheduler_thread_name(name):
            return False
        return True
    except Exception:
        return True


def resolve_effective_lock_policy(interval: int, timeout_sec: float, skip_if_busy: bool) -> tuple[float, bool, str]:
    try:
        interval = int(interval)
        timeout_sec = max(0.1, safe_float(timeout_sec, DEFAULT_LOCK_ACQUIRE_TIMEOUT_SEC))
        current_thread_name = thread_name()

        effective_timeout = timeout_sec
        effective_skip = bool(skip_if_busy)
        reason = "as_is"

        if interval == 1 and is_low_priority_interval1_writer(current_thread_name):
            if timeout_sec > 3.0:
                effective_timeout = 3.0
            if not effective_skip:
                effective_skip = True
            reason = "interval1_low_priority_auto_short_timeout_skip"

        return effective_timeout, effective_skip, reason

    except Exception:
        logger.exception("[SUMMARY] resolve effective lock policy failed")
        return timeout_sec, skip_if_busy, "policy_error"


def should_fast_skip_before_lock(interval: int, skip_if_busy: bool, timeout_sec: float) -> tuple[bool, str]:
    try:
        interval = int(interval)
        if interval != 1:
            return False, "interval_not_1"

        if not skip_if_busy:
            return False, "skip_if_busy_false"

        holder = lock_holder_snapshot(interval)
        if not holder:
            return False, "no_holder"

        holder_tid = safe_int(holder.get("holder_tid"), -1)
        holder_thread = str(holder.get("holder_thread") or "")
        holder_depth = safe_int(holder.get("depth"), 0)
        held_sec = safe_float(holder.get("held_sec"), 0.0)

        my_tid = thread_ident()
        my_thread = thread_name()

        if holder_tid == my_tid:
            return False, "same_thread_reentry"

        if is_scheduler_thread_name(my_thread):
            return False, "scheduler_writer_should_wait"

        threshold = min(max(0.5, timeout_sec * 0.5), 2.5)
        if held_sec >= threshold:
            return True, (
                f"holder_busy holder_tid={holder_tid} holder_thread={holder_thread} "
                f"holder_depth={holder_depth} held={held_sec:.3f}s threshold={threshold:.3f}s"
            )

        return False, "holder_not_long_enough"

    except Exception:
        logger.exception("[SUMMARY] fast skip decision failed interval=%s", interval)
        return False, "decision_error"


def update_lock_meta(interval: int, **kwargs: Any) -> None:
    interval = int(interval)
    with _INTERVAL_LOCK_META_GUARD:
        cur = dict(_INTERVAL_LOCK_META.get(interval, {}))
        cur.update(kwargs)
        _INTERVAL_LOCK_META[interval] = cur


@contextmanager
def acquire_interval_lock(
    interval: int,
    timeout_sec: float = DEFAULT_LOCK_ACQUIRE_TIMEOUT_SEC,
    raise_on_timeout: bool = True,
):
    interval = int(interval)
    timeout_sec = max(0.1, safe_float(timeout_sec, DEFAULT_LOCK_ACQUIRE_TIMEOUT_SEC))

    lock = get_interval_lock(interval)

    waiter_tid = thread_ident()
    waiter_tname = thread_name()
    wait_started_mono = time.monotonic()

    prev_meta = get_lock_meta(interval)
    if prev_meta:
        prev_holder_tid = prev_meta.get("holder_tid")
        prev_holder_name = prev_meta.get("holder_thread")
        prev_hold_started_mono = safe_float(prev_meta.get("hold_started_mono"), 0.0)
        prev_depth = safe_int(prev_meta.get("depth"), 0)
        prev_held_sec = max(0.0, time.monotonic() - prev_hold_started_mono) if prev_hold_started_mono > 0 else -1.0
        logger.debug(
            "[SUMMARY] interval lock waiting → interval=%s waiter_tid=%s waiter_thread=%s holder_tid=%s holder_thread=%s holder_depth=%s holder_held=%.3fs",
            interval,
            waiter_tid,
            waiter_tname,
            prev_holder_tid,
            prev_holder_name,
            prev_depth,
            prev_held_sec,
        )

    acquired = False
    hold_started_mono = 0.0

    try:
        acquired = lock.acquire(timeout=timeout_sec)
        waited_sec = time.monotonic() - wait_started_mono

        if not acquired:
            timeout_meta = get_lock_meta(interval)
            holder_tid = timeout_meta.get("holder_tid")
            holder_thread = timeout_meta.get("holder_thread")
            hold_started_mono = safe_float(timeout_meta.get("hold_started_mono"), 0.0)
            holder_depth = safe_int(timeout_meta.get("depth"), 0)
            holder_held_sec = max(0.0, time.monotonic() - hold_started_mono) if hold_started_mono > 0 else -1.0

            logger.error(
                "[SUMMARY] interval lock acquire timeout: interval=%s waited=%.3fs waiter_tid=%s waiter_thread=%s holder_tid=%s holder_thread=%s holder_depth=%s holder_held=%.3fs holder_acquired_at=%s",
                interval,
                waited_sec,
                waiter_tid,
                waiter_tname,
                holder_tid,
                holder_thread,
                holder_depth,
                holder_held_sec,
                timeout_meta.get("hold_started_wall"),
            )

            if raise_on_timeout:
                raise TimeoutError(
                    f"[SUMMARY] interval lock acquire timeout: interval={interval} waited={waited_sec:.3f}s"
                )

            yield False
            return

        hold_started_mono = time.monotonic()

        current_meta = get_lock_meta(interval)
        current_holder_tid = current_meta.get("holder_tid")
        current_depth = safe_int(current_meta.get("depth"), 0)

        if current_holder_tid == waiter_tid and current_depth > 0:
            update_lock_meta(interval, depth=current_depth + 1)
            logger.debug(
                "[SUMMARY] interval lock re-entered → interval=%s tid=%s thread=%s depth=%s waited=%.3fs",
                interval,
                waiter_tid,
                waiter_tname,
                current_depth + 1,
                waited_sec,
            )
        else:
            set_lock_meta(
                interval,
                {
                    "holder_tid": waiter_tid,
                    "holder_thread": waiter_tname,
                    "hold_started_wall": now_wall_str(),
                    "hold_started_mono": hold_started_mono,
                    "depth": 1,
                },
            )
            if waited_sec >= LOCK_WAIT_WARN_SEC:
                logger.warning(
                    "[SUMMARY] interval lock waited %.3fs → interval=%s tid=%s thread=%s",
                    waited_sec,
                    interval,
                    waiter_tid,
                    waiter_tname,
                )
            else:
                logger.debug(
                    "[SUMMARY] interval lock acquired immediately → interval=%s tid=%s thread=%s",
                    interval,
                    waiter_tid,
                    waiter_tname,
                )

        yield True

    finally:
        if acquired:
            meta_before_release = get_lock_meta(interval)
            depth_before_release = safe_int(meta_before_release.get("depth"), 1)
            held_sec = (
                max(
                    0.0,
                    time.monotonic() - safe_float(meta_before_release.get("hold_started_mono"), hold_started_mono),
                )
                if hold_started_mono > 0 else 0.0
            )

            try:
                if depth_before_release > 1:
                    update_lock_meta(interval, depth=depth_before_release - 1)
                    logger.debug(
                        "[SUMMARY] interval lock re-exit → interval=%s tid=%s thread=%s depth=%s held=%.3fs",
                        interval,
                        waiter_tid,
                        waiter_tname,
                        depth_before_release - 1,
                        held_sec,
                    )
                else:
                    if held_sec >= LOCK_HOLD_WARN_SEC:
                        logger.warning(
                            "[SUMMARY] interval lock held long %.3fs → interval=%s tid=%s thread=%s",
                            held_sec,
                            interval,
                            waiter_tid,
                            waiter_tname,
                        )
                    else:
                        logger.debug(
                            "[SUMMARY] interval lock released → interval=%s held=%.3fs tid=%s thread=%s",
                            interval,
                            held_sec,
                            waiter_tid,
                            waiter_tname,
                        )
                    clear_lock_meta(interval)
            except Exception:
                logger.exception("[SUMMARY] interval lock release logging/meta failed: interval=%s", interval)

            try:
                lock.release()
            except Exception:
                logger.exception("[SUMMARY] interval lock release failed: interval=%s")