# ============================================================
# File   : trading/entry_exit/tasks.py
# Version: Ver2.2-FIX-TONOSAMA-OVERLAP-TIMEOUT
# ------------------------------------------------------------
# 【目的】
#   core.entry_exit_tasks shim から解決される実体モジュール。
#
# Ver2.2 Fix:
#   - TONOSAMAが30秒周期なのに timeout 45s + controller 20s で
#     60秒超になり previous_still_running が出る問題を修正。
#   - TONOSAMA既定 build timeout を45秒→12秒へ短縮。
#   - timeout時の controller dispatch は既定OFF。
#   - timeoutで残ったdaemon threadが生きている間は次回TONOSAMA起動をskip。
#   - timeout後は短いcooldownを入れ、同時多重実行を防止。
#
# Ver2.1 Fix:
#   - _dispatch_entry_controller() の timeout ログで timeout_sec 引数が不足し、
#     logging error Message/Arguments が出ていた問題を修正。
#
# Ver2.0 Fix:
#   - 14:16ログで ranking_entry が4件pending作成まで到達したが、
#     実処理68秒に対してscheduler timeout=60秒が先に発生し、
#     entry_controller dispatch されなかった問題を修正
#   - RANKING_ENTRY_BUILD_TIMEOUT_SEC 既定を60秒→90秒へ延長
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

import schedule

logger = logging.getLogger(__name__)

_TAG_ENTRY = "entry"
_TAG_TONOSAMA_ENTRY = "tonosama_entry"
_TAG_RANKING_ENTRY = "ranking_entry"

_TONOSAMA_ENTRY_RUNNING = False
_TONOSAMA_ENTRY_STARTED_AT: Optional[dt.datetime] = None
_TONOSAMA_ENTRY_COOLDOWN_UNTIL: Optional[dt.datetime] = None
_TONOSAMA_ENTRY_TIMEOUT_STREAK = 0
_TONOSAMA_ENTRY_ORPHAN_THREAD: Optional[threading.Thread] = None
_TONOSAMA_ENTRY_LOCK = threading.RLock()

_RANKING_ENTRY_RUNNING = False
_RANKING_ENTRY_STARTED_AT: Optional[dt.datetime] = None
_RANKING_ENTRY_COOLDOWN_UNTIL: Optional[dt.datetime] = None
_RANKING_ENTRY_TIMEOUT_STREAK = 0
_RANKING_ENTRY_LOCK = threading.RLock()

TONOSAMA_ENTRY_TIMEOUT_SEC = float(os.getenv("TONOSAMA_ENTRY_TIMEOUT_SEC", "12"))
TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.getenv("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", "8"))
TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC = float(os.getenv("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC", "45"))
TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC = float(os.getenv("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "180"))
RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.getenv("RANKING_ENTRY_BUILD_TIMEOUT_SEC", "90"))
RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.getenv("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", "20"))
RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC = float(os.getenv("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "90"))
RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC = float(os.getenv("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "300"))


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _entry_source(entry: Any) -> str:
    try:
        if isinstance(entry, dict):
            return str(entry.get("source") or entry.get("pipeline_source") or entry.get("entry_type") or "").upper()
        return str(getattr(entry, "source", None) or getattr(entry, "pipeline_source", None) or getattr(entry, "entry_type", None) or "").upper()
    except Exception:
        return ""


def _pending_count_for_source(source: str) -> int:
    source_u = str(source or "").upper()
    total = 0
    try:
        import trading.entry.pending_manager as pm
        iter_entries = getattr(pm, "iter_entries", None)
        if callable(iter_entries):
            for _sym, entry in list(iter_entries()):
                if source_u in _entry_source(entry):
                    total += 1
            if total > 0:
                return int(total)
    except Exception:
        logger.debug("[entry_exit.tasks] pending count via iter_entries failed", exc_info=True)
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", None)
        if isinstance(root, dict):
            for bucket in list(root.values()):
                entries = bucket if isinstance(bucket, (list, tuple, set)) else [bucket]
                for entry in entries:
                    if source_u in _entry_source(entry):
                        total += 1
            return int(total)
    except Exception:
        logger.debug("[entry_exit.tasks] pending count via global_data failed", exc_info=True)
    try:
        import trading.entry.pending_manager as pm
        names = ["pending_entries", "PENDING_ENTRIES", "pending_by_symbol", "PENDING_BY_SYMBOL", "_pending_entries", "_PENDING_ENTRIES", "_pending_by_symbol", "_PENDING_BY_SYMBOL"]
        for name in names:
            obj = getattr(pm, name, None)
            if obj is None:
                continue
            if isinstance(obj, dict):
                vals = []
                for v in obj.values():
                    vals.extend(list(v) if isinstance(v, (list, tuple, set)) else [v])
                for item in vals:
                    if source_u in _entry_source(item):
                        total += 1
            elif isinstance(obj, (list, tuple, set)):
                for item in obj:
                    if source_u in _entry_source(item):
                        total += 1
        return int(total)
    except Exception:
        return int(total)


def _clear_tag(tag: str) -> None:
    try:
        schedule.clear(tag)
        logger.info("[entry_exit.tasks] schedule.clear tag=%s", tag)
    except Exception:
        logger.warning("[entry_exit.tasks] schedule.clear failed tag=%s", tag, exc_info=True)


def _has_tag(tag: str) -> bool:
    try:
        for job in list(getattr(schedule, "jobs", []) or []):
            if tag in (getattr(job, "tags", set()) or set()):
                return True
    except Exception:
        pass
    return False


def _resolve_callable(module_name: str, attr_name: str) -> Optional[Callable[..., Any]]:
    try:
        import importlib
        mod = importlib.import_module(module_name)
        fn = getattr(mod, attr_name, None)
        if callable(fn):
            logger.info("[entry_exit.tasks] resolved %s.%s", module_name, attr_name)
            return fn
        logger.warning("[entry_exit.tasks] callable not found %s.%s", module_name, attr_name)
        return None
    except Exception:
        logger.warning("[entry_exit.tasks] resolve failed %s.%s", module_name, attr_name, exc_info=True)
        return None


def _patch_tonosama_runner_fast_loop() -> None:
    try:
        if _env_bool("TONOSAMA_UPDATE_ACTIVE_SYMBOLS_IN_LOOP", False):
            logger.info("[TONOSAMA FAST LOOP PATCH] keep update_active_symbols because TONOSAMA_UPDATE_ACTIVE_SYMBOLS_IN_LOOP=1")
            return
        if not _env_bool("TONOSAMA_ENTRY_FAST_SKIP_ACTIVE_UPDATE", True):
            logger.info("[TONOSAMA FAST LOOP PATCH] disabled by TONOSAMA_ENTRY_FAST_SKIP_ACTIVE_UPDATE=0")
            return
        import importlib
        mod = importlib.import_module("trading.entry.tonosama.runner")
        cur = getattr(mod, "update_active_symbols", None)
        if cur is not None:
            setattr(mod, "update_active_symbols", None)
            logger.warning("[TONOSAMA FAST LOOP PATCH] runner.update_active_symbols disabled for fast loop")
    except Exception:
        logger.warning("[TONOSAMA FAST LOOP PATCH] failed", exc_info=True)


def _run_callable_with_timeout_thread(
    fn: Callable[..., Any],
    *,
    timeout_sec: float,
    name: str,
    args: tuple[Any, ...] = (),
    kwargs: Optional[dict[str, Any]] = None,
) -> tuple[bool, Any, Optional[threading.Thread]]:
    result: dict[str, Any] = {"done": False, "ret": None, "err": None}
    kwargs = kwargs or {}

    def _target() -> None:
        try:
            result["ret"] = fn(*args, **kwargs)
            result["done"] = True
        except Exception as e:
            result["err"] = e
            result["done"] = True

    th = threading.Thread(target=_target, daemon=True, name=f"entry-timeout-{name}")
    th.start()
    th.join(max(0.1, float(timeout_sec or 0.1)))
    if th.is_alive():
        logger.warning("[%s] timeout -> return to scheduler timeout_sec=%.3f thread_alive=True", name, timeout_sec)
        return False, None, th
    if result.get("err") is not None:
        raise result["err"]
    return True, result.get("ret"), None


def _run_callable_with_timeout(fn: Callable[..., Any], *, timeout_sec: float, name: str, args: tuple[Any, ...] = (), kwargs: Optional[dict[str, Any]] = None) -> tuple[bool, Any]:
    completed, ret, _th = _run_callable_with_timeout_thread(fn, timeout_sec=timeout_sec, name=name, args=args, kwargs=kwargs)
    return completed, ret


def _dispatch_entry_controller(*, pipeline_source: str, interval: int | None, timeout_sec: float, reason: str) -> bool:
    controller_fn = _resolve_callable("trading.handlers.entry_controller", "run_entry_pipeline")
    if not callable(controller_fn):
        logger.warning("[%s] entry_controller unavailable pipeline_source=%s", reason, pipeline_source)
        return False
    kwargs: dict[str, Any] = {"pipeline_source": pipeline_source}
    if interval is not None:
        kwargs["interval"] = interval
    logger.info("[%s] dispatch entry_controller pipeline_source=%s interval=%s timeout_sec=%.3f", reason, pipeline_source, interval, timeout_sec)
    completed, _ret = _run_callable_with_timeout(controller_fn, timeout_sec=timeout_sec, name=f"{reason} CONTROLLER", kwargs=kwargs)
    if not completed:
        logger.warning("[%s] controller timeout pipeline_source=%s interval=%s timeout_sec=%.3f", reason, pipeline_source, interval, timeout_sec)
        return False
    logger.info("[%s] controller done pipeline_source=%s interval=%s", reason, pipeline_source, interval)
    return True


def _tonosama_entry_cooldown_seconds() -> float:
    try:
        streak = max(1, int(_TONOSAMA_ENTRY_TIMEOUT_STREAK or 1))
        base = max(1.0, float(TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC))
        max_sec = max(base, float(TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC))
        return min(max_sec, base * streak)
    except Exception:
        return 45.0


def _run_tonosama_entry_safe() -> int:
    global _TONOSAMA_ENTRY_RUNNING, _TONOSAMA_ENTRY_STARTED_AT, _TONOSAMA_ENTRY_COOLDOWN_UNTIL, _TONOSAMA_ENTRY_TIMEOUT_STREAK, _TONOSAMA_ENTRY_ORPHAN_THREAD
    started_dt = dt.datetime.now()
    started = time.perf_counter()
    with _TONOSAMA_ENTRY_LOCK:
        if _TONOSAMA_ENTRY_COOLDOWN_UNTIL is not None and started_dt < _TONOSAMA_ENTRY_COOLDOWN_UNTIL:
            remain = (_TONOSAMA_ENTRY_COOLDOWN_UNTIL - started_dt).total_seconds()
            logger.warning("[TONOSAMA ENTRY SCHEDULE] skipped reason=timeout_cooldown remain=%.1fs until=%s timeout_streak=%s", remain, _TONOSAMA_ENTRY_COOLDOWN_UNTIL, _TONOSAMA_ENTRY_TIMEOUT_STREAK)
            return 0
        if _TONOSAMA_ENTRY_ORPHAN_THREAD is not None and _TONOSAMA_ENTRY_ORPHAN_THREAD.is_alive():
            logger.warning("[TONOSAMA ENTRY SCHEDULE] skipped reason=previous_timeout_thread_still_alive thread=%s", _TONOSAMA_ENTRY_ORPHAN_THREAD.name)
            return 0
        _TONOSAMA_ENTRY_ORPHAN_THREAD = None
        if _TONOSAMA_ENTRY_RUNNING:
            elapsed = (dt.datetime.now() - _TONOSAMA_ENTRY_STARTED_AT).total_seconds() if _TONOSAMA_ENTRY_STARTED_AT else None
            logger.warning("[TONOSAMA ENTRY SCHEDULE] skipped reason=previous_still_running started_at=%s elapsed=%s", _TONOSAMA_ENTRY_STARTED_AT, elapsed)
            return 0
        _TONOSAMA_ENTRY_RUNNING = True
        _TONOSAMA_ENTRY_STARTED_AT = started_dt

    fn = _resolve_callable("trading.entry.tonosama.runner", "tonosama_loop")
    if not callable(fn):
        logger.warning("[TONOSAMA ENTRY SCHEDULE] skipped reason=runner_unavailable")
        with _TONOSAMA_ENTRY_LOCK:
            _TONOSAMA_ENTRY_RUNNING = False
            _TONOSAMA_ENTRY_STARTED_AT = None
        return 0

    try:
        _patch_tonosama_runner_fast_loop()
        before_pending = _pending_count_for_source("TONOSAMA")
        logger.info("[TONOSAMA ENTRY SCHEDULE] fire timeout_sec=%.3f before_pending=%s", TONOSAMA_ENTRY_TIMEOUT_SEC, before_pending)
        completed, ret, timeout_thread = _run_callable_with_timeout_thread(fn, timeout_sec=TONOSAMA_ENTRY_TIMEOUT_SEC, name="TONOSAMA ENTRY SCHEDULE")
        after_pending = _pending_count_for_source("TONOSAMA")
        if not completed:
            with _TONOSAMA_ENTRY_LOCK:
                _TONOSAMA_ENTRY_TIMEOUT_STREAK += 1
                _TONOSAMA_ENTRY_ORPHAN_THREAD = timeout_thread
                cool_sec = _tonosama_entry_cooldown_seconds()
                _TONOSAMA_ENTRY_COOLDOWN_UNTIL = dt.datetime.now() + dt.timedelta(seconds=cool_sec)
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] build timeout -> cooldown timeout_sec=%.3f elapsed=%.3fs pending_count=%s timeout_streak=%s cooldown_sec=%.1f until=%s dispatch_on_timeout_pending=%s",
                TONOSAMA_ENTRY_TIMEOUT_SEC,
                time.perf_counter() - started,
                after_pending,
                _TONOSAMA_ENTRY_TIMEOUT_STREAK,
                cool_sec,
                _TONOSAMA_ENTRY_COOLDOWN_UNTIL,
                _env_bool("TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING", False),
            )
            if after_pending > before_pending and _env_bool("TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING", False):
                _dispatch_entry_controller(pipeline_source="TONOSAMA", interval=None, timeout_sec=TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC, reason="TONOSAMA ENTRY SCHEDULE TIMEOUT-PENDING")
            return 0

        _TONOSAMA_ENTRY_TIMEOUT_STREAK = 0
        _TONOSAMA_ENTRY_COOLDOWN_UNTIL = None
        _TONOSAMA_ENTRY_ORPHAN_THREAD = None
        registered = int(ret or 0)
        logger.info("[TONOSAMA ENTRY SCHEDULE] pending build done registered=%s before_pending=%s after_pending=%s elapsed=%.3fs", registered, before_pending, after_pending, time.perf_counter() - started)
        if registered > 0 or after_pending > before_pending:
            _dispatch_entry_controller(pipeline_source="TONOSAMA", interval=None, timeout_sec=TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC, reason="TONOSAMA ENTRY SCHEDULE")
        else:
            logger.info("[TONOSAMA ENTRY SCHEDULE] no new pending created -> controller dispatch skipped before_pending=%s after_pending=%s", before_pending, after_pending)
        logger.info("[TONOSAMA ENTRY SCHEDULE] done result=%s pending_count=%s elapsed=%.3fs", registered, after_pending, time.perf_counter() - started)
        return registered
    except Exception:
        logger.exception("[TONOSAMA ENTRY SCHEDULE] failed")
        return 0
    finally:
        with _TONOSAMA_ENTRY_LOCK:
            _TONOSAMA_ENTRY_RUNNING = False
            _TONOSAMA_ENTRY_STARTED_AT = None


def _ranking_entry_cooldown_seconds() -> float:
    try:
        streak = max(1, int(_RANKING_ENTRY_TIMEOUT_STREAK or 1))
        base = max(1.0, float(RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC))
        max_sec = max(base, float(RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC))
        return min(max_sec, base * streak)
    except Exception:
        return 90.0


def _run_ranking_entry_safe() -> int:
    global _RANKING_ENTRY_RUNNING, _RANKING_ENTRY_STARTED_AT, _RANKING_ENTRY_COOLDOWN_UNTIL, _RANKING_ENTRY_TIMEOUT_STREAK
    started_dt = dt.datetime.now()
    started = time.perf_counter()
    with _RANKING_ENTRY_LOCK:
        if _RANKING_ENTRY_COOLDOWN_UNTIL is not None and started_dt < _RANKING_ENTRY_COOLDOWN_UNTIL:
            remain = (_RANKING_ENTRY_COOLDOWN_UNTIL - started_dt).total_seconds()
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=timeout_cooldown remain=%.1fs until=%s timeout_streak=%s", remain, _RANKING_ENTRY_COOLDOWN_UNTIL, _RANKING_ENTRY_TIMEOUT_STREAK)
            return 0
        if _RANKING_ENTRY_RUNNING:
            elapsed = (dt.datetime.now() - _RANKING_ENTRY_STARTED_AT).total_seconds() if _RANKING_ENTRY_STARTED_AT else None
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=previous_still_running started_at=%s elapsed=%s", _RANKING_ENTRY_STARTED_AT, elapsed)
            return 0
        _RANKING_ENTRY_RUNNING = True
        _RANKING_ENTRY_STARTED_AT = started_dt
    try:
        logger.info("[RANKING ENTRY SCHEDULE] fire at=%s", started_dt.strftime("%Y-%m-%d %H:%M:%S"))
        build_fn = _resolve_callable("trading.ranking.entry_from_ranking", "run_ranking_entry_pipeline")
        if not callable(build_fn):
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=ranking_entry_pipeline_unavailable")
            return 0
        completed, created_ret = _run_callable_with_timeout(build_fn, timeout_sec=RANKING_ENTRY_BUILD_TIMEOUT_SEC, name="RANKING ENTRY BUILD")
        if not completed:
            with _RANKING_ENTRY_LOCK:
                _RANKING_ENTRY_TIMEOUT_STREAK += 1
                cool_sec = _ranking_entry_cooldown_seconds()
                _RANKING_ENTRY_COOLDOWN_UNTIL = dt.datetime.now() + dt.timedelta(seconds=cool_sec)
            logger.warning("[RANKING ENTRY SCHEDULE] build timeout -> cooldown timeout_sec=%.3f elapsed=%.3fs timeout_streak=%s cooldown_sec=%.1f until=%s", RANKING_ENTRY_BUILD_TIMEOUT_SEC, time.perf_counter() - started, _RANKING_ENTRY_TIMEOUT_STREAK, cool_sec, _RANKING_ENTRY_COOLDOWN_UNTIL)
            return 0
        _RANKING_ENTRY_TIMEOUT_STREAK = 0
        _RANKING_ENTRY_COOLDOWN_UNTIL = None
        created = int(created_ret or 0)
        logger.info("[RANKING ENTRY SCHEDULE] pending build done created=%s", created)
        if created > 0:
            _dispatch_entry_controller(pipeline_source="RANKING", interval=1, timeout_sec=RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC, reason="RANKING ENTRY SCHEDULE")
        else:
            logger.info("[RANKING ENTRY SCHEDULE] no pending created -> controller dispatch skipped")
        logger.info("[RANKING ENTRY SCHEDULE] done created=%s elapsed=%.3fs", created, time.perf_counter() - started)
        return created
    except Exception:
        logger.exception("[RANKING ENTRY SCHEDULE] failed")
        return 0
    finally:
        with _RANKING_ENTRY_LOCK:
            _RANKING_ENTRY_RUNNING = False
            _RANKING_ENTRY_STARTED_AT = None


def _resolve_tonosama_interval_sec() -> int:
    try:
        env_v = os.getenv("TONOSAMA_ENTRY_INTERVAL_SEC")
        if env_v is not None and str(env_v).strip() != "":
            return max(10, int(float(env_v)))
    except Exception:
        pass
    try:
        from trading.entry.tonosama.config import SCHEDULER_INTERVAL_SEC
        return max(30, int(SCHEDULER_INTERVAL_SEC or 30))
    except Exception:
        return 30


def _resolve_ranking_entry_interval_min() -> int:
    try:
        env_v = os.getenv("RANKING_ENTRY_INTERVAL_MIN")
        if env_v is not None and str(env_v).strip() != "":
            return max(1, int(float(env_v)))
    except Exception:
        pass
    return 2


def register_entry_exit_tasks(*args: Any, **kwargs: Any) -> bool:
    try:
        logger.info("[entry_exit.tasks] register_entry_exit_tasks start")
        _clear_tag(_TAG_TONOSAMA_ENTRY)
        _clear_tag(_TAG_RANKING_ENTRY)
        interval_sec = _resolve_tonosama_interval_sec()
        job_t = schedule.every(interval_sec).seconds.do(_run_tonosama_entry_safe)
        job_t.tag(_TAG_ENTRY)
        job_t.tag(_TAG_TONOSAMA_ENTRY)
        ranking_interval_min = _resolve_ranking_entry_interval_min()
        if ranking_interval_min <= 1:
            job_r = schedule.every().minute.at(":12").do(_run_ranking_entry_safe)
        else:
            job_r = schedule.every(ranking_interval_min).minutes.at(":12").do(_run_ranking_entry_safe)
        job_r.tag(_TAG_ENTRY)
        job_r.tag(_TAG_RANKING_ENTRY)
        logger.info(
            "[entry_exit.tasks] registered tonosama every=%ss tag=%s build_timeout=%.1fs controller_timeout=%.1fs timeout_cooldown=%.1f-%0.1fs dispatch_timeout_pending=%s ranking every=%smin at :12 tag=%s build_timeout=%.1fs controller_timeout=%.1fs cooldown=%.1f-%0.1fs pending_count_global=True",
            interval_sec, _TAG_TONOSAMA_ENTRY, TONOSAMA_ENTRY_TIMEOUT_SEC, TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC,
            TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC, TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC,
            _env_bool("TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING", False),
            ranking_interval_min, _TAG_RANKING_ENTRY, RANKING_ENTRY_BUILD_TIMEOUT_SEC, RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC,
            RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC, RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC,
        )
        ok = _has_tag(_TAG_TONOSAMA_ENTRY) and _has_tag(_TAG_RANKING_ENTRY)
        logger.info("[entry_exit.tasks] register_entry_exit_tasks done ok=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[entry_exit.tasks] register_entry_exit_tasks failed")
        return False


def register_jobs(*args: Any, **kwargs: Any) -> bool:
    return register_entry_exit_tasks(*args, **kwargs)


def setup_entry_exit_tasks(*args: Any, **kwargs: Any) -> bool:
    return register_entry_exit_tasks(*args, **kwargs)


def start_entry_exit_tasks(*args: Any, **kwargs: Any) -> bool:
    return register_entry_exit_tasks(*args, **kwargs)


__all__ = ["register_entry_exit_tasks", "register_jobs", "setup_entry_exit_tasks", "start_entry_exit_tasks"]
