# ============================================================
# File   : core/startup/summary_push_bg_due_interval_guard_patch.py
# Version: V4-ONE-MINUTE-DEDICATED-THREAD-DISPLAY-FIRST
# ------------------------------------------------------------
# 目的:
#   main.py(entry_only) で PUSH 1m/3m/5m をBG実行する際、
#   3分足・5分足を毎分投入しないようにしつつ、1分足は確実に表示する。
#
# V4 修正:
#   ✔ 1分足専用 daemon Thread は維持
#   ✔ main.py側の1分足PUSHは、計算後に保存処理で止まらないよう
#     summary_main_skip_save_for_display_patch を同時installする
#   ✔ DB保存は main_database.py 側が担当し、main.py は表示/AIを優先
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_SUBMIT = None
_RUNNING_BY_INTERVAL: dict[int, tuple[float, str]] = {}
_LOCK = threading.RLock()


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return max(1.0, float(raw))
    except Exception:
        return float(default)


def _stale_sec_for_interval(interval: int) -> float:
    iv = int(interval)
    if iv == 1:
        return _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_1M", 60.0)
    if iv == 3:
        return _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_3M", _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0))
    if iv == 5:
        return _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_5M", _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0))
    return _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0)


def _is_due(interval: int, now: dt.datetime) -> bool:
    try:
        iv = int(interval)
        if iv <= 1:
            return True
        return int(now.minute) % iv == 0
    except Exception:
        return True


def _install_display_first_patch() -> bool:
    try:
        from core.startup.summary_main_skip_save_for_display_patch import install as install_display_first
        ok = bool(install_display_first())
        logger.warning("[SUMMARY PUSH BG DUE GUARD] display-first save skip patch installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY PUSH BG DUE GUARD] display-first save skip patch install failed")
        return False


def _run_task_direct_thread(task, *, bg_key: str, interval: int) -> bool:
    try:
        th = threading.Thread(
            target=task,
            name=f"summary-push-{int(interval)}m-{bg_key}",
            daemon=True,
        )
        th.start()
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] bg push dedicated thread started key=%s interval=%s thread=%s",
            bg_key,
            interval,
            th.name,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY PUSH BG DUE GUARD] dedicated thread start failed key=%s interval=%s", bg_key, interval)
        return False


def _patched_submit_bg_push_interval(*, interval: int, now: dt.datetime, display: bool, run_entry: bool) -> None:
    try:
        import core.startup.summary_parallel_intervals_runtime_patch as sp
    except Exception:
        logger.exception("[SUMMARY PUSH BG DUE GUARD] import summary_parallel failed")
        if callable(_ORIGINAL_SUBMIT):
            return _ORIGINAL_SUBMIT(interval=interval, now=now, display=display, run_entry=run_entry)
        return None

    iv = int(interval)
    due_only = _env_bool("SUMMARY_PUSH_BG_LONG_INTERVAL_DUE_ONLY", True)
    if due_only and iv in (3, 5) and not _is_due(iv, now):
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] skipped reason=not_due interval=%s now=%s minute=%s",
            iv,
            now,
            getattr(now, "minute", None),
        )
        return None

    stale_sec = _stale_sec_for_interval(iv)
    now_ts = time.time()
    with _LOCK:
        old = _RUNNING_BY_INTERVAL.get(iv)
        if old:
            started, key_old = old
            elapsed = now_ts - float(started)
            if elapsed < stale_sec:
                logger.warning(
                    "[SUMMARY PUSH BG DUE GUARD] skipped reason=interval_still_running interval=%s elapsed=%.1fs stale_sec=%.1fs key=%s",
                    iv,
                    elapsed,
                    stale_sec,
                    key_old,
                )
                return None

            logger.warning(
                "[SUMMARY PUSH BG DUE GUARD] stale interval running cleared interval=%s elapsed=%.1fs stale_sec=%.1fs key=%s",
                iv,
                elapsed,
                stale_sec,
                key_old,
            )
            _RUNNING_BY_INTERVAL.pop(iv, None)

        bg_key = f"{now.strftime('%Y%m%d%H%M')}:push:{iv}"
        _RUNNING_BY_INTERVAL[iv] = (now_ts, bg_key)

    def _task() -> None:
        try:
            logger.warning(
                "[SUMMARY PUSH BG DUE GUARD] bg push start key=%s interval=%s now=%s display=%s run_entry=%s stale_sec=%.1f dedicated=%s",
                bg_key,
                iv,
                now,
                display,
                run_entry,
                stale_sec,
                iv == 1,
            )
            sp._job_one_source(source="push", interval=iv, now=now, display=display, run_entry=run_entry)
        except Exception:
            logger.exception("[SUMMARY PUSH BG DUE GUARD] bg push failed key=%s interval=%s", bg_key, iv)
        finally:
            with _LOCK:
                cur = _RUNNING_BY_INTERVAL.get(iv)
                if cur and cur[1] == bg_key:
                    _RUNNING_BY_INTERVAL.pop(iv, None)
            logger.warning("[SUMMARY PUSH BG DUE GUARD] bg push done key=%s interval=%s now=%s", bg_key, iv, now)

    try:
        if iv == 1 and _env_bool("SUMMARY_PUSH_1M_DEDICATED_THREAD", True):
            ok = _run_task_direct_thread(_task, bg_key=bg_key, interval=iv)
            if not ok:
                with _LOCK:
                    cur = _RUNNING_BY_INTERVAL.get(iv)
                    if cur and cur[1] == bg_key:
                        _RUNNING_BY_INTERVAL.pop(iv, None)
            return None

        sp._bg_executor().submit(_task)
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] bg push submitted key=%s interval=%s now=%s stale_sec=%.1f",
            bg_key,
            iv,
            now,
            stale_sec,
        )
    except Exception:
        with _LOCK:
            cur = _RUNNING_BY_INTERVAL.get(iv)
            if cur and cur[1] == bg_key:
                _RUNNING_BY_INTERVAL.pop(iv, None)
        logger.exception("[SUMMARY PUSH BG DUE GUARD] submit failed key=%s interval=%s", bg_key, iv)


def install() -> bool:
    global _PATCHED, _ORIGINAL_SUBMIT
    if _PATCHED:
        _install_display_first_patch()
        return True
    try:
        import core.startup.summary_parallel_intervals_runtime_patch as sp
        cur = getattr(sp, "_submit_bg_push_interval", None)
        if getattr(cur, "_summary_push_bg_due_guard_v4", False):
            _PATCHED = True
            _install_display_first_patch()
            return True
        _ORIGINAL_SUBMIT = cur
        _patched_submit_bg_push_interval._summary_push_bg_due_guard = True  # type: ignore[attr-defined]
        _patched_submit_bg_push_interval._summary_push_bg_due_guard_v4 = True  # type: ignore[attr-defined]
        sp._submit_bg_push_interval = _patched_submit_bg_push_interval
        _PATCHED = True
        display_first_ok = _install_display_first_patch()
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] installed v4 due_only=%s stale_1m=%.1f stale_3m=%.1f stale_5m=%.1f dedicated_1m=%s display_first=%s original=%s",
            _env_bool("SUMMARY_PUSH_BG_LONG_INTERVAL_DUE_ONLY", True),
            _stale_sec_for_interval(1),
            _stale_sec_for_interval(3),
            _stale_sec_for_interval(5),
            _env_bool("SUMMARY_PUSH_1M_DEDICATED_THREAD", True),
            display_first_ok,
            getattr(cur, "__name__", str(cur)),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY PUSH BG DUE GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY PUSH BG DUE GUARD] auto install failed")


__all__ = ["install"]
