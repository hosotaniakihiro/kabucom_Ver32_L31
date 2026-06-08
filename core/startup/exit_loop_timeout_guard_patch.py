# ============================================================
# File   : core/startup/exit_loop_timeout_guard_patch.py
# Version: V2.0-EXIT-LOOP-ORPHAN-WORKER-REPLACE
# ------------------------------------------------------------
# Purpose:
#   exit_loop_5s が broker/API/DB 読み込みなどで長時間固まり、
#   previous worker still alive のまま新しい exit worker を起動できず、
#   建玉監視・利確損切が止まる問題を防ぐ。
#
# Behavior:
#   - scheduler_exit_bootstrap.run_exit_loop_market_guarded を短時間で戻るwrapperへ差し替える。
#   - 実EXIT処理は daemon worker thread で実行。
#   - worker が EXIT_LOOP_RUN_TIMEOUT_SEC を超えたら scheduler 側は戻る。
#   - worker が EXIT_LOOP_ORPHAN_REPLACE_SEC を超えてまだ生きている場合、
#     古い worker は orphan として参照を切り、新しい worker を起動する。
#
# ENV:
#   EXIT_LOOP_TIMEOUT_GUARD_ENABLED=1
#   EXIT_LOOP_RUN_TIMEOUT_SEC=4.0
#   EXIT_LOOP_STUCK_WARN_SEC=8.0
#   EXIT_LOOP_ORPHAN_REPLACE_SEC=45.0
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_INSTALLED = False
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_STARTED_AT: float | None = None
_WORKER_SEQ = 0
_ORPHANED_WORKERS: list[tuple[int, str, float]] = []


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


def _clear_worker_ref(reason: str, seq_hint: int | None = None) -> None:
    global _WORKER_THREAD, _WORKER_STARTED_AT
    try:
        th = _WORKER_THREAD
        age = _worker_age()
        name = getattr(th, "name", "") if th is not None else ""
        seq = int(seq_hint or _WORKER_SEQ)
        if th is not None and th.is_alive():
            _ORPHANED_WORKERS.append((seq, name, age))
            del _ORPHANED_WORKERS[:-10]
        logger.warning(
            "[EXIT LOOP TIMEOUT GUARD] clear worker ref reason=%s seq=%s age=%.3fs thread=%s orphaned_count=%s",
            reason,
            seq,
            age,
            name,
            len(_ORPHANED_WORKERS),
        )
    except Exception:
        logger.debug("[EXIT LOOP TIMEOUT GUARD] clear worker ref failed", exc_info=True)
    _WORKER_THREAD = None
    _WORKER_STARTED_AT = None


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
        if getattr(current, "_exit_loop_timeout_guard_v2", False):
            _INSTALLED = True
            return True
        original = getattr(current, "_original", current)
        if not callable(original):
            logger.warning("[EXIT LOOP TIMEOUT GUARD] original run_exit_loop_market_guarded not callable")
            return False

        def _run_worker(seq: int) -> None:
            global _WORKER_THREAD, _WORKER_STARTED_AT
            try:
                logger.info("[EXIT LOOP TIMEOUT GUARD] worker start seq=%s", seq)
                original()
                logger.info("[EXIT LOOP TIMEOUT GUARD] worker done seq=%s elapsed=%.3fs", seq, _worker_age())
            except Exception:
                logger.exception("[EXIT LOOP TIMEOUT GUARD] worker failed seq=%s", seq)
            finally:
                with _WORKER_LOCK:
                    try:
                        cur_name = getattr(_WORKER_THREAD, "name", "") if _WORKER_THREAD is not None else ""
                        if cur_name == f"exit-loop-worker-timeout-guard-{seq}":
                            _WORKER_THREAD = None
                            _WORKER_STARTED_AT = None
                            logger.info("[EXIT LOOP TIMEOUT GUARD] worker ref cleared on finish seq=%s", seq)
                    except Exception:
                        pass

        def _patched_run_exit_loop_market_guarded() -> None:
            global _WORKER_THREAD, _WORKER_STARTED_AT, _WORKER_SEQ
            timeout_sec = _env_float("EXIT_LOOP_RUN_TIMEOUT_SEC", 4.0)
            stuck_warn_sec = _env_float("EXIT_LOOP_STUCK_WARN_SEC", 8.0)
            orphan_replace_sec = _env_float("EXIT_LOOP_ORPHAN_REPLACE_SEC", 45.0)

            with _WORKER_LOCK:
                if _worker_alive():
                    age = _worker_age()
                    if age >= orphan_replace_sec:
                        logger.error(
                            "[EXIT LOOP TIMEOUT GUARD] previous worker orphaned -> start replacement age=%.3fs replace_sec=%.3fs old_thread=%s",
                            age,
                            orphan_replace_sec,
                            getattr(_WORKER_THREAD, "name", ""),
                        )
                        _clear_worker_ref("orphan_replace")
                    else:
                        if age >= stuck_warn_sec:
                            logger.warning(
                                "[EXIT LOOP TIMEOUT GUARD] previous worker still alive -> skip new worker age=%.3fs timeout=%.3fs replace_sec=%.3fs thread=%s",
                                age,
                                timeout_sec,
                                orphan_replace_sec,
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
            if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
                logger.warning(
                    "[EXIT LOOP TIMEOUT GUARD] worker timeout -> release scheduler seq=%s elapsed=%.3fs timeout=%.3fs replace_sec=%.3fs",
                    seq,
                    _worker_age(),
                    timeout_sec,
                    orphan_replace_sec,
                )
                return
            logger.info("[EXIT LOOP TIMEOUT GUARD] worker completed within timeout seq=%s elapsed=%.3fs", seq, _worker_age())

        _patched_run_exit_loop_market_guarded._exit_loop_timeout_guard_v1 = True  # type: ignore[attr-defined]
        _patched_run_exit_loop_market_guarded._exit_loop_timeout_guard_v2 = True  # type: ignore[attr-defined]
        _patched_run_exit_loop_market_guarded._original = original  # type: ignore[attr-defined]
        boot.run_exit_loop_market_guarded = _patched_run_exit_loop_market_guarded

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
        os.environ.setdefault("EXIT_LOOP_ORPHAN_REPLACE_SEC", "45.0")

        _INSTALLED = True
        logger.warning(
            "[EXIT LOOP TIMEOUT GUARD] installed v2 timeout=%s stuck_warn=%s orphan_replace=%s",
            os.environ.get("EXIT_LOOP_RUN_TIMEOUT_SEC"),
            os.environ.get("EXIT_LOOP_STUCK_WARN_SEC"),
            os.environ.get("EXIT_LOOP_ORPHAN_REPLACE_SEC"),
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
