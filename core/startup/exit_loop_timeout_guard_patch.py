# ============================================================
# File   : core/startup/exit_loop_timeout_guard_patch.py
# Version: V1.0-EXIT-LOOP-SCHEDULER-TIMEOUT-GUARD
# ------------------------------------------------------------
# Purpose:
#   exit_loop_5s が broker/API/DB 読み込みなどで長時間固まり、
#   schedule_loop 側が previous still running で次回EXITを捨て続ける問題を防ぐ。
#
# Behavior:
#   - scheduler_exit_bootstrap.run_exit_loop_market_guarded を短時間で戻るwrapperへ差し替える。
#   - 実EXIT処理は daemon worker thread で実行。
#   - worker が EXIT_LOOP_RUN_TIMEOUT_SEC を超えたら scheduler 側は戻る。
#   - まだ古いworkerが動いている間は、新しいworkerは起動しない。
#   - これにより schedule_loop の running key を長時間占有しない。
#
# ENV:
#   EXIT_LOOP_TIMEOUT_GUARD_ENABLED=1
#   EXIT_LOOP_RUN_TIMEOUT_SEC=4.0
#   EXIT_LOOP_STUCK_WARN_SEC=8.0
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_STARTED_AT: float | None = None
_WORKER_SEQ = 0


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return max(0.1, float(v))
    except Exception:
        return float(default)


def _worker_alive() -> bool:
    try:
        return _WORKER_THREAD is not None and _WORKER_THREAD.is_alive()
    except Exception:
        return False


def _worker_age() -> float:
    try:
        if _WORKER_STARTED_AT is None:
            return 0.0
        return max(0.0, time.time() - float(_WORKER_STARTED_AT))
    except Exception:
        return 0.0


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("EXIT_LOOP_TIMEOUT_GUARD_ENABLED", True):
        logger.warning("[EXIT LOOP TIMEOUT GUARD] disabled by env")
        return False
    try:
        import core.startup.scheduler_exit_bootstrap as boot
        current = getattr(boot, "run_exit_loop_market_guarded", None)
        if getattr(current, "_exit_loop_timeout_guard_v1", False):
            _INSTALLED = True
            return True
        if not callable(current):
            logger.warning("[EXIT LOOP TIMEOUT GUARD] original run_exit_loop_market_guarded not callable")
            return False

        original = current

        def _run_worker(seq: int) -> None:
            try:
                logger.info("[EXIT LOOP TIMEOUT GUARD] worker start seq=%s", seq)
                original()
                logger.info("[EXIT LOOP TIMEOUT GUARD] worker done seq=%s elapsed=%.3fs", seq, _worker_age())
            except Exception:
                logger.exception("[EXIT LOOP TIMEOUT GUARD] worker failed seq=%s", seq)

        def _patched_run_exit_loop_market_guarded() -> None:
            global _WORKER_THREAD, _WORKER_STARTED_AT, _WORKER_SEQ
            timeout_sec = _env_float("EXIT_LOOP_RUN_TIMEOUT_SEC", 4.0)
            stuck_warn_sec = _env_float("EXIT_LOOP_STUCK_WARN_SEC", 8.0)

            with _WORKER_LOCK:
                if _worker_alive():
                    age = _worker_age()
                    if age >= stuck_warn_sec:
                        logger.warning(
                            "[EXIT LOOP TIMEOUT GUARD] previous worker still alive -> skip new worker age=%.3fs timeout=%.3fs thread=%s",
                            age,
                            timeout_sec,
                            getattr(_WORKER_THREAD, "name", ""),
                        )
                    else:
                        logger.info(
                            "[EXIT LOOP TIMEOUT GUARD] worker already running -> skip new worker age=%.3fs",
                            age,
                        )
                    return

                _WORKER_SEQ += 1
                seq = _WORKER_SEQ
                _WORKER_STARTED_AT = time.time()
                _WORKER_THREAD = threading.Thread(
                    target=_run_worker,
                    args=(seq,),
                    name=f"exit-loop-worker-timeout-guard-{seq}",
                    daemon=True,
                )
                _WORKER_THREAD.start()

            _WORKER_THREAD.join(timeout=timeout_sec)
            if _WORKER_THREAD.is_alive():
                logger.warning(
                    "[EXIT LOOP TIMEOUT GUARD] worker timeout -> release scheduler seq=%s elapsed=%.3fs timeout=%.3fs",
                    seq,
                    _worker_age(),
                    timeout_sec,
                )
                return
            logger.info("[EXIT LOOP TIMEOUT GUARD] worker completed within timeout seq=%s elapsed=%.3fs", seq, _worker_age())

        _patched_run_exit_loop_market_guarded._exit_loop_timeout_guard_v1 = True  # type: ignore[attr-defined]
        _patched_run_exit_loop_market_guarded._original = original  # type: ignore[attr-defined]
        boot.run_exit_loop_market_guarded = _patched_run_exit_loop_market_guarded

        # 既存schedule登録済みjobのfunc参照も差し替える。
        try:
            import schedule
            replaced = 0
            for job in list(getattr(schedule, "jobs", []) or []):
                try:
                    if "exit_loop_5s" in set(getattr(job, "tags", set()) or set()):
                        job.job_func = _patched_run_exit_loop_market_guarded
                        replaced += 1
                except Exception:
                    pass
            logger.warning("[EXIT LOOP TIMEOUT GUARD] replaced scheduled jobs count=%s", replaced)
        except Exception:
            logger.debug("[EXIT LOOP TIMEOUT GUARD] schedule job replace failed", exc_info=True)

        os.environ.setdefault("EXIT_LOOP_RUN_TIMEOUT_SEC", "4.0")
        os.environ.setdefault("EXIT_LOOP_STUCK_WARN_SEC", "8.0")

        _INSTALLED = True
        logger.warning(
            "[EXIT LOOP TIMEOUT GUARD] installed timeout=%s stuck_warn=%s",
            os.environ.get("EXIT_LOOP_RUN_TIMEOUT_SEC"),
            os.environ.get("EXIT_LOOP_STUCK_WARN_SEC"),
        )
        return True
    except Exception:
        logger.exception("[EXIT LOOP TIMEOUT GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[EXIT LOOP TIMEOUT GUARD] auto install failed")


__all__ = ["install"]
