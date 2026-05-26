# ============================================================
# File   : trading/entry_exit/tasks.py
# Version: Ver1.5-TONOSAMA-LONGER-TIMEOUT-DISPATCH-PENDING
# ------------------------------------------------------------
# 【目的】
#   core.entry_exit_tasks shim から解決される実体モジュール。
#
# 【登録するジョブ】
#   - 殿様イナゴ候補生成:
#       trading.entry.tonosama.runner.tonosama_loop
#       15秒ごと / tags: entry, tonosama_entry
#       候補 pending 登録後、即 entry_controller.run_entry_pipeline(TONOSAMA)
#       へ流して実発注まで到達させる。
#
#   - ランキング由来エントリー:
#       trading.ranking.entry_from_ranking.run_ranking_entry_pipeline
#       毎分 :12 / tags: entry, ranking_entry
#       その直後に entry_controller.run_entry_pipeline(pipeline_source="RANKING", interval=1)
#
# Ver1.5 Fix:
#   - 最新ログで TONOSAMA の候補生成が 38秒前後かかり、旧timeout=12秒で
#     [TONOSAMA ENTRY SCHEDULE] timeout skipped_result になっていた。
#   - その結果、runner側で pending added しても、schedule側は result=0 扱いになり
#     entry_controller dispatch へ進まないケースがあった。
#   - 既定 timeout を 45秒へ延長。
#   - ret が 0/None でも pending が存在する場合は controller dispatch する。
#
# Ver1.4 Fix:
#   - TONOSAMA内の update_active_symbols() を既定スキップし、15秒監視を軽量化。
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

_RANKING_ENTRY_RUNNING = False
_RANKING_ENTRY_STARTED_AT: Optional[dt.datetime] = None
_RANKING_ENTRY_LOCK = threading.RLock()

TONOSAMA_ENTRY_TIMEOUT_SEC = float(os.getenv("TONOSAMA_ENTRY_TIMEOUT_SEC", "45"))
TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.getenv("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", "20"))
RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.getenv("RANKING_ENTRY_BUILD_TIMEOUT_SEC", "20"))
RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.getenv("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", "20"))


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_len(obj: Any) -> int:
    try:
        return len(obj) if obj is not None else 0
    except Exception:
        return 0


def _pending_count_for_source(source: str) -> int:
    """pending_manager に残っている指定sourceの件数をできるだけ安全に数える。"""
    try:
        import trading.entry.pending_manager as pm
        source_u = str(source or "").upper()
        names = [
            "pending_entries",
            "PENDING_ENTRIES",
            "pending_by_symbol",
            "PENDING_BY_SYMBOL",
            "_pending_entries",
            "_PENDING_ENTRIES",
            "_pending_by_symbol",
            "_PENDING_BY_SYMBOL",
        ]
        total = 0
        for name in names:
            obj = getattr(pm, name, None)
            if obj is None:
                continue
            if isinstance(obj, dict):
                vals = []
                for v in obj.values():
                    if isinstance(v, (list, tuple, set)):
                        vals.extend(list(v))
                    else:
                        vals.append(v)
                for item in vals:
                    try:
                        s = str(getattr(item, "source", None) or getattr(item, "pipeline_source", None) or "").upper()
                        if not s and isinstance(item, dict):
                            s = str(item.get("source") or item.get("pipeline_source") or "").upper()
                        if source_u in s:
                            total += 1
                    except Exception:
                        pass
            elif isinstance(obj, (list, tuple, set)):
                for item in obj:
                    try:
                        s = str(getattr(item, "source", None) or getattr(item, "pipeline_source", None) or "").upper()
                        if not s and isinstance(item, dict):
                            s = str(item.get("source") or item.get("pipeline_source") or "").upper()
                        if source_u in s:
                            total += 1
                    except Exception:
                        pass
        return int(total)
    except Exception:
        return 0


def _clear_tag(tag: str) -> None:
    try:
        schedule.clear(tag)
        logger.info("[entry_exit.tasks] schedule.clear tag=%s", tag)
    except Exception:
        logger.warning("[entry_exit.tasks] schedule.clear failed tag=%s", tag, exc_info=True)


def _has_tag(tag: str) -> bool:
    try:
        for job in list(getattr(schedule, "jobs", []) or []):
            tags = getattr(job, "tags", set()) or set()
            if tag in tags:
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
    """
    TONOSAMA 15秒ジョブの中で active_symbols 更新を毎回走らせない。
    """
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


def _run_callable_with_timeout(
    fn: Callable[..., Any],
    *,
    timeout_sec: float,
    name: str,
    args: tuple[Any, ...] = (),
    kwargs: Optional[dict[str, Any]] = None,
) -> tuple[bool, Any]:
    result: dict[str, Any] = {"done": False, "ret": None, "err": None}
    kwargs = kwargs or {}

    def _target() -> None:
        try:
            result["ret"] = fn(*args, **kwargs)
            result["done"] = True
        except Exception as e:  # noqa: BLE001
            result["err"] = e
            result["done"] = True

    th = threading.Thread(target=_target, daemon=True, name=f"entry-timeout-{name}")
    th.start()
    th.join(max(0.1, float(timeout_sec or 0.1)))

    if th.is_alive():
        logger.warning(
            "[%s] timeout -> return to scheduler timeout_sec=%.3f thread_alive=True",
            name,
            timeout_sec,
        )
        return False, None

    if result.get("err") is not None:
        raise result["err"]

    return True, result.get("ret")


def _dispatch_entry_controller(*, pipeline_source: str, interval: int | None, timeout_sec: float, reason: str) -> bool:
    controller_fn = _resolve_callable("trading.handlers.entry_controller", "run_entry_pipeline")
    if not callable(controller_fn):
        logger.warning("[%s] entry_controller unavailable pipeline_source=%s", reason, pipeline_source)
        return False

    kwargs: dict[str, Any] = {"pipeline_source": pipeline_source}
    if interval is not None:
        kwargs["interval"] = interval

    logger.info(
        "[%s] dispatch entry_controller pipeline_source=%s interval=%s timeout_sec=%.3f",
        reason,
        pipeline_source,
        interval,
        timeout_sec,
    )
    completed, _ret = _run_callable_with_timeout(
        controller_fn,
        timeout_sec=timeout_sec,
        name=f"{reason} CONTROLLER",
        kwargs=kwargs,
    )
    if not completed:
        logger.warning(
            "[%s] controller timeout pipeline_source=%s interval=%s timeout_sec=%.3f",
            reason,
            pipeline_source,
            interval,
            timeout_sec,
        )
        return False
    logger.info("[%s] controller done pipeline_source=%s interval=%s", reason, pipeline_source, interval)
    return True


def _run_tonosama_entry_safe() -> int:
    started = time.perf_counter()
    fn = _resolve_callable("trading.entry.tonosama.runner", "tonosama_loop")
    if not callable(fn):
        logger.warning("[TONOSAMA ENTRY SCHEDULE] skipped reason=runner_unavailable")
        return 0
    try:
        _patch_tonosama_runner_fast_loop()
        logger.info("[TONOSAMA ENTRY SCHEDULE] fire timeout_sec=%.3f", TONOSAMA_ENTRY_TIMEOUT_SEC)
        completed, ret = _run_callable_with_timeout(
            fn,
            timeout_sec=TONOSAMA_ENTRY_TIMEOUT_SEC,
            name="TONOSAMA ENTRY SCHEDULE",
        )
        if not completed:
            pending_count = _pending_count_for_source("TONOSAMA")
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] timeout skipped_result elapsed=%.3fs pending_count=%s",
                time.perf_counter() - started,
                pending_count,
            )
            if pending_count > 0 and _env_bool("TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING", True):
                _dispatch_entry_controller(
                    pipeline_source="TONOSAMA",
                    interval=None,
                    timeout_sec=TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC,
                    reason="TONOSAMA ENTRY SCHEDULE TIMEOUT-PENDING",
                )
            return 0

        registered = int(ret or 0)
        pending_count = _pending_count_for_source("TONOSAMA")
        logger.info(
            "[TONOSAMA ENTRY SCHEDULE] pending build done registered=%s pending_count=%s elapsed=%.3fs",
            registered,
            pending_count,
            time.perf_counter() - started,
        )

        if registered > 0 or pending_count > 0:
            _dispatch_entry_controller(
                pipeline_source="TONOSAMA",
                interval=None,
                timeout_sec=TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC,
                reason="TONOSAMA ENTRY SCHEDULE",
            )
        else:
            logger.info("[TONOSAMA ENTRY SCHEDULE] no pending created -> controller dispatch skipped")

        logger.info("[TONOSAMA ENTRY SCHEDULE] done result=%s pending_count=%s elapsed=%.3fs", registered, pending_count, time.perf_counter() - started)
        return registered
    except Exception:
        logger.exception("[TONOSAMA ENTRY SCHEDULE] failed")
        return 0


def _run_ranking_entry_safe() -> int:
    global _RANKING_ENTRY_RUNNING
    global _RANKING_ENTRY_STARTED_AT

    started_dt = dt.datetime.now()
    started = time.perf_counter()

    with _RANKING_ENTRY_LOCK:
        if _RANKING_ENTRY_RUNNING:
            elapsed = None
            if _RANKING_ENTRY_STARTED_AT is not None:
                elapsed = (dt.datetime.now() - _RANKING_ENTRY_STARTED_AT).total_seconds()
            logger.warning(
                "[RANKING ENTRY SCHEDULE] skipped reason=previous_still_running started_at=%s elapsed=%s",
                _RANKING_ENTRY_STARTED_AT,
                elapsed,
            )
            return 0
        _RANKING_ENTRY_RUNNING = True
        _RANKING_ENTRY_STARTED_AT = started_dt

    try:
        logger.info("[RANKING ENTRY SCHEDULE] fire at=%s", started_dt.strftime("%Y-%m-%d %H:%M:%S"))

        build_fn = _resolve_callable("trading.ranking.entry_from_ranking", "run_ranking_entry_pipeline")
        if not callable(build_fn):
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=ranking_entry_pipeline_unavailable")
            return 0

        completed, created_ret = _run_callable_with_timeout(
            build_fn,
            timeout_sec=RANKING_ENTRY_BUILD_TIMEOUT_SEC,
            name="RANKING ENTRY BUILD",
        )
        if not completed:
            logger.warning(
                "[RANKING ENTRY SCHEDULE] build timeout -> controller dispatch skipped timeout_sec=%.3f elapsed=%.3fs",
                RANKING_ENTRY_BUILD_TIMEOUT_SEC,
                time.perf_counter() - started,
            )
            return 0

        created = int(created_ret or 0)
        logger.info("[RANKING ENTRY SCHEDULE] pending build done created=%s", created)

        if created > 0:
            _dispatch_entry_controller(
                pipeline_source="RANKING",
                interval=1,
                timeout_sec=RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC,
                reason="RANKING ENTRY SCHEDULE",
            )
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


def register_entry_exit_tasks(*args: Any, **kwargs: Any) -> bool:
    """
    schedule ライブラリへ entry 系ジョブを登録する。
    core.entry_exit_tasks.register_entry_exit_tasks から委譲される。
    """
    try:
        logger.info("[entry_exit.tasks] register_entry_exit_tasks start")

        _clear_tag(_TAG_TONOSAMA_ENTRY)
        _clear_tag(_TAG_RANKING_ENTRY)

        interval_sec = 15
        try:
            from trading.entry.tonosama.config import SCHEDULER_INTERVAL_SEC
            interval_sec = max(5, int(SCHEDULER_INTERVAL_SEC or 15))
        except Exception:
            interval_sec = 15

        job_t = schedule.every(interval_sec).seconds.do(_run_tonosama_entry_safe)
        job_t.tag(_TAG_ENTRY)
        job_t.tag(_TAG_TONOSAMA_ENTRY)

        job_r = schedule.every().minute.at(":12").do(_run_ranking_entry_safe)
        job_r.tag(_TAG_ENTRY)
        job_r.tag(_TAG_RANKING_ENTRY)

        logger.info(
            "[entry_exit.tasks] registered tonosama every=%ss tag=%s build_timeout=%.1fs controller_timeout=%.1fs ranking every minute at :12 tag=%s build_timeout=%.1fs controller_timeout=%.1fs",
            interval_sec,
            _TAG_TONOSAMA_ENTRY,
            TONOSAMA_ENTRY_TIMEOUT_SEC,
            TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC,
            _TAG_RANKING_ENTRY,
            RANKING_ENTRY_BUILD_TIMEOUT_SEC,
            RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC,
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


__all__ = [
    "register_entry_exit_tasks",
    "register_jobs",
    "setup_entry_exit_tasks",
    "start_entry_exit_tasks",
]
