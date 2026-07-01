# ============================================================
# File   : core/scheduler_tasks.py
# Version: Ver32.3-RANKING-SAVE-DB-RUNTIME-ONLY
# ------------------------------------------------------------
# Function:
#   - アプリ全体の scheduler タスク登録を担当する
#   - summary系 scheduler の登録を統合親tick優先で実行する
#   - ranking_snapshot_1min 保存タスクは main_database.py 側だけで登録する
#   - main.py は発注/監視優先のため ranking 保存を登録しない
#   - import 半壊時も利用可能な関数だけ登録し、全体停止を避ける
#
# Ver32.3:
#   - main.py側で ranking_save_tick が登録され、20秒timeoutしていた問題を修正
#   - RANKING_SAVE_IN_MAIN_ENABLED の既定を main.py では 0 相当に変更
#   - main_database.py / DB writer runtime では既定で ranking 保存を有効化
#   - RANKING_SAVE_FORCE_ENABLED=1 で任意プロセスでも強制有効化可能
#
# ENV:
#   RANKING_SAVE_IN_MAIN_ENABLED=1/0      main.py override。default 0 in main.py
#   RANKING_SAVE_DB_RUNTIME_ENABLED=1/0   DB runtime default 1
#   RANKING_SAVE_FORCE_ENABLED=1/0        force enable in any process default 0
#   RANKING_SAVE_INTERVAL_MIN=3           default 3
#   RANKING_SAVE_TIMEOUT_SEC=20           default 20
#   RANKING_SAVE_TIMEOUT_COOLDOWN_SEC=180 default 180
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Optional

import schedule

logger = logging.getLogger(__name__)

_TAG_SUMMARY_FALLBACK_TICK = "summary_fallback_tick"
_TAG_RANKING_SAVE_TICK = "ranking_save_tick"
_RANKING_SAVE_SECOND = 2

_RANKING_SAVE_LOCK = threading.RLock()
_RANKING_SAVE_COOLDOWN_UNTIL: dt.datetime | None = None
_RANKING_SAVE_TIMEOUT_STREAK = 0


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in (sys.argv or []))
    except Exception:
        return ""


def _is_main_database_runtime() -> bool:
    argv = _argv_text()
    if "main_database.py" in argv or "main_database" in argv:
        return True
    role = str(os.getenv("KABU_RUNTIME_ROLE") or os.getenv("SUMMARY_RUNTIME_ROLE") or os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
    return role in {"database", "db", "db_writer", "writer", "main_database", "summary_db_writer", "ranking_db_writer"}


def _is_main_entry_runtime() -> bool:
    argv = _argv_text()
    if "main_database.py" in argv or "main_database" in argv:
        return False
    if "main.py" in argv:
        return True
    role = str(os.getenv("KABU_RUNTIME_ROLE") or os.getenv("SUMMARY_RUNTIME_ROLE") or os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
    return role in {"main", "entry", "entry_only", "main_entry", "main_entry_only", "read_only", "no_save"}


def _ranking_save_enabled_for_runtime() -> bool:
    """ranking保存は原則 main_database.py だけで実行する。"""
    if _env_bool("RANKING_SAVE_FORCE_ENABLED", False):
        return True
    if _is_main_database_runtime():
        return _env_bool("RANKING_SAVE_DB_RUNTIME_ENABLED", True)
    if _is_main_entry_runtime():
        return _env_bool("RANKING_SAVE_IN_MAIN_ENABLED", False)
    # どちらか判別できない場合は安全側で無効。DB runtimeは role か main_database.py で明示する。
    return _env_bool("RANKING_SAVE_UNKNOWN_RUNTIME_ENABLED", False)


def _resolve_attr(module_name: str, attr_name: str) -> Optional[Callable[..., Any]]:
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, attr_name, None)
        if callable(fn):
            logger.info("[core.scheduler_tasks] resolved %s.%s", module_name, attr_name)
            return fn
        logger.info("[core.scheduler_tasks] unresolved %s.%s (not callable)", module_name, attr_name)
        return None
    except Exception:
        logger.info("[core.scheduler_tasks] unresolved %s.%s", module_name, attr_name, exc_info=False)
        return None


def _call_with_supported_kwargs(fn: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_var_kw:
            return fn(**kwargs)
        call_kwargs = {k: v for k, v in kwargs.items() if k in params}
        return fn(**call_kwargs)
    except ValueError:
        return fn(**kwargs)


def _call_with_timeout(fn: Callable[[], Any], *, timeout_sec: float, name: str) -> tuple[bool, Any]:
    result: dict[str, Any] = {"done": False, "ret": None, "err": None}

    def _target() -> None:
        try:
            result["ret"] = fn()
            result["done"] = True
        except Exception as e:
            result["err"] = e
            result["done"] = True

    th = threading.Thread(target=_target, daemon=True, name=f"scheduler-timeout-{name}")
    th.start()
    th.join(max(0.1, float(timeout_sec or 0.1)))
    if th.is_alive():
        logger.warning("[%s] timeout -> return to scheduler timeout_sec=%.3f thread_alive=True", name, timeout_sec)
        return False, None
    if result.get("err") is not None:
        raise result["err"]
    return True, result.get("ret")


def _safe_call(fn: Optional[Callable[..., Any]], name: str) -> None:
    try:
        if callable(fn):
            fn()
            logger.info("[core.scheduler_tasks] %s ok", name)
        else:
            logger.info("[core.scheduler_tasks] %s skipped (not available)", name)
    except Exception:
        logger.exception("[core.scheduler_tasks] %s failed", name)


def _has_schedule_tag(tag: str) -> bool:
    try:
        for job in list(getattr(schedule, "jobs", []) or []):
            tags = getattr(job, "tags", set()) or set()
            if tag in tags:
                return True
    except Exception:
        pass
    return False


def _clear_schedule_tag(tag: str) -> None:
    try:
        schedule.clear(tag)
        logger.info("[core.scheduler_tasks] cleared existing scheduled jobs tag=%s", tag)
    except Exception:
        logger.warning("[core.scheduler_tasks] schedule.clear failed tag=%s", tag, exc_info=True)


def _log_registered_jobs(context: str) -> None:
    try:
        rows = []
        for job in list(getattr(schedule, "jobs", []) or []):
            try:
                rows.append({
                    "job": str(job),
                    "tags": sorted(list(getattr(job, "tags", set()) or set())),
                    "next_run": str(getattr(job, "next_run", None)),
                    "last_run": str(getattr(job, "last_run", None)),
                })
            except Exception:
                rows.append({"job": str(job)})
        logger.info("[core.scheduler_tasks] schedule snapshot context=%s count=%s jobs=%s", context, len(rows), rows)
    except Exception:
        logger.debug("[core.scheduler_tasks] schedule snapshot failed context=%s", context, exc_info=True)


_register_push_summary_tasks = _resolve_attr("scheduler_jobs.summary.scheduler", "register_push_summary_tasks")
_register_ranking_summary_tasks = _resolve_attr("scheduler_jobs.summary.scheduler", "register_ranking_summary_tasks")
_register_summary_tasks_impl = _resolve_attr("scheduler_jobs.summary.scheduler", "register_summary_tasks")
_register_time_locked_summary_tasks = _resolve_attr("scheduler_jobs.summary.scheduler", "register_time_locked_summary_tasks")
_job_push_summary = _resolve_attr("scheduler_jobs.summary.runners", "job_summary")
_job_ranking_summary = _resolve_attr("scheduler_jobs.summary.runners", "job_ranking_summary")
_job_save_ranking = _resolve_attr("trading.ranking.scheduler", "job_save_ranking")
_save_ranking_data_loop = _resolve_attr("trading.ranking.scheduler", "save_ranking_data_loop")
register_yahoo_tasks = _resolve_attr("core.yahoo_tasks", "register_yahoo_tasks")
register_push_tasks = _resolve_attr("core.push_tasks", "register_push_tasks")
register_entry_exit_tasks = _resolve_attr("core.entry_exit_tasks", "register_entry_exit_tasks")


def _run_push_interval(interval: int) -> None:
    try:
        if callable(_job_push_summary):
            _job_push_summary(int(interval))
            logger.info("[core.scheduler_tasks] push summary fired interval=%s", interval)
        else:
            logger.warning("[core.scheduler_tasks] push summary runner unavailable interval=%s", interval)
    except Exception:
        logger.exception("[core.scheduler_tasks] push summary failed interval=%s", interval)


def _run_ranking_interval(interval: int) -> None:
    try:
        if callable(_job_ranking_summary):
            _job_ranking_summary(int(interval))
            logger.info("[core.scheduler_tasks] ranking summary fired interval=%s", interval)
        else:
            logger.warning("[core.scheduler_tasks] ranking summary runner unavailable interval=%s", interval)
    except Exception:
        logger.exception("[core.scheduler_tasks] ranking summary failed interval=%s", interval)


def _summary_tick() -> None:
    try:
        now = dt.datetime.now().replace(second=0, microsecond=0)
        minute = int(now.minute)
        logger.info("[core.scheduler_tasks] summary tick start hhmm=%s interval_base_minute=%s", now.strftime("%H:%M"), minute)
        _run_push_interval(1)
        if minute % 3 == 0:
            _run_push_interval(3)
        if minute % 5 == 0:
            _run_push_interval(5)
        _run_ranking_interval(1)
        if minute % 3 == 0:
            _run_ranking_interval(3)
        if minute % 5 == 0:
            _run_ranking_interval(5)
        logger.info("[core.scheduler_tasks] summary tick finished")
    except Exception:
        logger.exception("[core.scheduler_tasks] summary tick failed")


def _register_summary_fallback_tasks() -> None:
    try:
        _clear_schedule_tag(_TAG_SUMMARY_FALLBACK_TICK)
        schedule.every().minute.at(":00").do(_summary_tick).tag(_TAG_SUMMARY_FALLBACK_TICK)
        logger.info("[core.scheduler_tasks] fallback summary schedule registered every minute :00")
    except Exception:
        logger.exception("[core.scheduler_tasks] fallback summary schedule registration failed")


def _ranking_save_cooldown_seconds() -> float:
    base = max(1.0, _env_float("RANKING_SAVE_TIMEOUT_COOLDOWN_SEC", 180.0))
    max_sec = max(base, _env_float("RANKING_SAVE_TIMEOUT_COOLDOWN_MAX_SEC", 600.0))
    streak = max(1, int(_RANKING_SAVE_TIMEOUT_STREAK or 1))
    return min(max_sec, base * streak)


def _run_ranking_save_tick() -> None:
    """ranking_snapshot_1min 保存用tick。timeout/cooldown付き。"""
    global _RANKING_SAVE_COOLDOWN_UNTIL, _RANKING_SAVE_TIMEOUT_STREAK
    started_dt = dt.datetime.now()
    started = time.perf_counter()

    if not _ranking_save_enabled_for_runtime():
        logger.info(
            "[core.scheduler_tasks] ranking save skipped disabled runtime main=%s main_database=%s argv=%s",
            _is_main_entry_runtime(), _is_main_database_runtime(), _argv_text(),
        )
        return

    with _RANKING_SAVE_LOCK:
        if _RANKING_SAVE_COOLDOWN_UNTIL is not None and started_dt < _RANKING_SAVE_COOLDOWN_UNTIL:
            remain = (_RANKING_SAVE_COOLDOWN_UNTIL - started_dt).total_seconds()
            logger.warning(
                "[core.scheduler_tasks] ranking save skipped reason=timeout_cooldown remain=%.1fs until=%s streak=%s",
                remain, _RANKING_SAVE_COOLDOWN_UNTIL, _RANKING_SAVE_TIMEOUT_STREAK,
            )
            return

    now = started_dt.replace(second=0, microsecond=0)
    timeout_sec = max(1.0, _env_float("RANKING_SAVE_TIMEOUT_SEC", 20.0))
    logger.info("[core.scheduler_tasks] ranking save tick start hhmm=%s timeout_sec=%.1f runtime=main_database", now.strftime("%H:%M"), timeout_sec)

    def _body() -> Any:
        if callable(_job_save_ranking):
            return _call_with_supported_kwargs(_job_save_ranking, mode="fast", run_full_postprocess=False, save_legacy=False)
        if callable(_save_ranking_data_loop):
            return _call_with_supported_kwargs(_save_ranking_data_loop, mode="fast", run_full_postprocess=False, save_legacy=False)
        logger.warning("[core.scheduler_tasks] ranking save runner unavailable fn=job_save_ranking/save_ranking_data_loop")
        return None

    try:
        completed, result = _call_with_timeout(_body, timeout_sec=timeout_sec, name="RANKING SAVE TICK")
        if not completed:
            with _RANKING_SAVE_LOCK:
                _RANKING_SAVE_TIMEOUT_STREAK += 1
                cool_sec = _ranking_save_cooldown_seconds()
                _RANKING_SAVE_COOLDOWN_UNTIL = dt.datetime.now() + dt.timedelta(seconds=cool_sec)
            logger.warning(
                "[core.scheduler_tasks] ranking save timeout -> cooldown elapsed=%.3fs streak=%s cooldown_sec=%.1f until=%s",
                time.perf_counter() - started, _RANKING_SAVE_TIMEOUT_STREAK, cool_sec, _RANKING_SAVE_COOLDOWN_UNTIL,
            )
            return

        with _RANKING_SAVE_LOCK:
            _RANKING_SAVE_TIMEOUT_STREAK = 0
            _RANKING_SAVE_COOLDOWN_UNTIL = None
        logger.info(
            "[core.scheduler_tasks] ranking save fired FAST result_type=%s elapsed=%.3fs",
            type(result).__name__, time.perf_counter() - started,
        )
        logger.info("[core.scheduler_tasks] ranking save tick finished hhmm=%s", now.strftime("%H:%M"))
    except Exception:
        logger.exception("[core.scheduler_tasks] ranking save tick failed")


def _resolve_ranking_save_interval_min() -> int:
    return max(1, _env_int("RANKING_SAVE_INTERVAL_MIN", 3))


def register_ranking_save_tasks() -> None:
    """ranking_snapshot_1min 保存タスク登録。main_database.pyだけで登録する。"""
    try:
        _clear_schedule_tag(_TAG_RANKING_SAVE_TICK)
        enabled = _ranking_save_enabled_for_runtime()
        if not enabled:
            logger.warning(
                "[core.scheduler_tasks] ranking save task not registered disabled runtime main=%s main_database=%s force=%s argv=%s",
                _is_main_entry_runtime(),
                _is_main_database_runtime(),
                os.getenv("RANKING_SAVE_FORCE_ENABLED"),
                _argv_text(),
            )
            return

        at_text = f":{int(_RANKING_SAVE_SECOND):02d}"
        interval_min = _resolve_ranking_save_interval_min()
        if interval_min <= 1:
            job = schedule.every().minute.at(at_text).do(_run_ranking_save_tick)
        else:
            job = schedule.every(interval_min).minutes.at(at_text).do(_run_ranking_save_tick)
        job.tag(_TAG_RANKING_SAVE_TICK)

        logger.info(
            "[core.scheduler_tasks] registered ranking save every %s minute(s) at %s tag=%s timeout=%.1fs cooldown=%.1fs runtime_main_database=%s job_save_available=%s loop_available=%s",
            interval_min,
            at_text,
            _TAG_RANKING_SAVE_TICK,
            _env_float("RANKING_SAVE_TIMEOUT_SEC", 20.0),
            _env_float("RANKING_SAVE_TIMEOUT_COOLDOWN_SEC", 180.0),
            _is_main_database_runtime(),
            callable(_job_save_ranking),
            callable(_save_ranking_data_loop),
        )
        if not callable(_job_save_ranking) and not callable(_save_ranking_data_loop):
            logger.warning("[core.scheduler_tasks] ranking save task registered but runner unavailable")
    except Exception:
        logger.exception("[core.scheduler_tasks] register_ranking_save_tasks failed")


def ensure_ranking_save_tasks_registered() -> None:
    try:
        if not _ranking_save_enabled_for_runtime():
            _clear_schedule_tag(_TAG_RANKING_SAVE_TICK)
            logger.warning("[core.scheduler_tasks] ranking save ensure skipped disabled for runtime; cleared tag=%s", _TAG_RANKING_SAVE_TICK)
            return
        if _has_schedule_tag(_TAG_RANKING_SAVE_TICK):
            logger.info("[core.scheduler_tasks] ranking save task already registered tag=%s", _TAG_RANKING_SAVE_TICK)
            return
        logger.warning("[core.scheduler_tasks] ranking save task not found. registering now tag=%s", _TAG_RANKING_SAVE_TICK)
        register_ranking_save_tasks()
    except Exception:
        logger.exception("[core.scheduler_tasks] ensure_ranking_save_tasks_registered failed")


def register_summary_only_tasks() -> None:
    try:
        logger.info("[core.scheduler_tasks] register_summary_only_tasks start")
        registered = False
        if callable(_register_summary_tasks_impl):
            try:
                _register_summary_tasks_impl()
                logger.info("[core.scheduler_tasks] register_summary_tasks impl ok")
                registered = True
            except Exception:
                logger.exception("[core.scheduler_tasks] register_summary_tasks impl failed")
        if (not registered) and callable(_register_time_locked_summary_tasks):
            try:
                _register_time_locked_summary_tasks()
                logger.info("[core.scheduler_tasks] register_time_locked_summary_tasks ok")
                registered = True
            except Exception:
                logger.exception("[core.scheduler_tasks] register_time_locked_summary_tasks failed")
        if not registered:
            dedicated_registered = False
            if callable(_register_push_summary_tasks):
                try:
                    _register_push_summary_tasks()
                    logger.info("[core.scheduler_tasks] register_push_summary_tasks ok")
                    dedicated_registered = True
                except Exception:
                    logger.exception("[core.scheduler_tasks] register_push_summary_tasks failed")
            if callable(_register_ranking_summary_tasks):
                try:
                    _register_ranking_summary_tasks()
                    logger.info("[core.scheduler_tasks] register_ranking_summary_tasks ok")
                    dedicated_registered = True
                except Exception:
                    logger.exception("[core.scheduler_tasks] register_ranking_summary_tasks failed")
            registered = dedicated_registered
        if not registered:
            _register_summary_fallback_tasks()
        logger.info("[core.scheduler_tasks] register_summary_only_tasks finished")
        _log_registered_jobs("after_register_summary_only_tasks")
    except Exception:
        logger.exception("[core.scheduler_tasks] register_summary_only_tasks failed")


def register_summary_tasks_compat() -> None:
    try:
        logger.info("[core.scheduler_tasks] register_summary_tasks_compat start")
        register_summary_only_tasks()
        register_ranking_save_tasks()
        logger.info("[core.scheduler_tasks] register_summary_tasks_compat finished")
        _log_registered_jobs("after_register_summary_tasks_compat")
    except Exception:
        logger.exception("[core.scheduler_tasks] register_summary_tasks_compat failed")


def register_summary_entry_exit_tasks() -> None:
    try:
        logger.info("[core.scheduler_tasks] register_summary_entry_exit_tasks start")
        register_summary_only_tasks()
        register_ranking_save_tasks()
        _safe_call(register_push_tasks, "register_push_tasks")
        _safe_call(register_yahoo_tasks, "register_yahoo_tasks")
        _safe_call(register_entry_exit_tasks, "register_entry_exit_tasks")
        ensure_ranking_save_tasks_registered()
        logger.info("[core.scheduler_tasks] register_summary_entry_exit_tasks finished")
        _log_registered_jobs("after_register_summary_entry_exit_tasks")
    except Exception:
        logger.exception("[core.scheduler_tasks] register_summary_entry_exit_tasks failed")


def register_summary_tasks() -> None:
    try:
        logger.info("[core.scheduler_tasks] register_summary_tasks compat start")
        register_summary_only_tasks()
        register_ranking_save_tasks()
        logger.info("[core.scheduler_tasks] register_summary_tasks compat finished")
        _log_registered_jobs("after_register_summary_tasks")
    except Exception:
        logger.exception("[core.scheduler_tasks] register_summary_tasks compat failed")


def register_all_tasks() -> None:
    register_summary_entry_exit_tasks()


__all__ = [
    "register_summary_only_tasks",
    "register_summary_tasks_compat",
    "register_summary_entry_exit_tasks",
    "register_summary_tasks",
    "register_ranking_save_tasks",
    "ensure_ranking_save_tasks_registered",
    "register_all_tasks",
]
