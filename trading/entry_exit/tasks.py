# ============================================================
# File   : trading/entry_exit/tasks.py
# Version: Ver1.0-ENTRY-EXIT-SCHEDULER-TASKS-RANKING-TONOSAMA
# ------------------------------------------------------------
# 【目的】
#   core.entry_exit_tasks shim から解決される実体モジュール。
#
# 【登録するジョブ】
#   - 殿様イナゴ候補生成:
#       trading.entry.tonosama.runner.tonosama_loop
#       15秒ごと / tags: entry, tonosama_entry
#
#   - ランキング由来エントリー:
#       trading.ranking.entry_from_ranking.run_ranking_entry_pipeline
#       毎分 :12 / tags: entry, ranking_entry
#       その直後に entry_controller.run_entry_pipeline(pipeline_source="RANKING", interval=1)
#
# 【重要】
#   - ランキングサマリー保存とランキング由来エントリーは別物。
#   - これを登録しないと ranking_snapshot は保存されても発注候補に流れない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
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


def _run_tonosama_entry_safe() -> int:
    started = time.perf_counter()
    fn = _resolve_callable("trading.entry.tonosama.runner", "tonosama_loop")
    if not callable(fn):
        logger.warning("[TONOSAMA ENTRY SCHEDULE] skipped reason=runner_unavailable")
        return 0
    try:
        logger.info("[TONOSAMA ENTRY SCHEDULE] fire")
        ret = fn()
        logger.info("[TONOSAMA ENTRY SCHEDULE] done result=%s elapsed=%.3fs", ret, time.perf_counter() - started)
        return int(ret or 0)
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

        created = int(build_fn() or 0)
        logger.info("[RANKING ENTRY SCHEDULE] pending build done created=%s", created)

        if created > 0:
            controller_fn = _resolve_callable("trading.handlers.entry_controller", "run_entry_pipeline")
            if callable(controller_fn):
                logger.info("[RANKING ENTRY SCHEDULE] dispatch entry_controller pipeline_source=RANKING interval=1")
                controller_fn(pipeline_source="RANKING", interval=1)
            else:
                logger.warning("[RANKING ENTRY SCHEDULE] entry_controller unavailable after pending created=%s", created)
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

        # 殿様イナゴ: config の秒数に追従。失敗時は15秒。
        interval_sec = 15
        try:
            from trading.entry.tonosama.config import SCHEDULER_INTERVAL_SEC
            interval_sec = max(5, int(SCHEDULER_INTERVAL_SEC or 15))
        except Exception:
            interval_sec = 15

        job_t = schedule.every(interval_sec).seconds.do(_run_tonosama_entry_safe)
        job_t.tag(_TAG_ENTRY)
        job_t.tag(_TAG_TONOSAMA_ENTRY)

        # ランキング由来エントリー: ranking save(:02), ranking summary(:10) の後ろで :12 に実行
        job_r = schedule.every().minute.at(":12").do(_run_ranking_entry_safe)
        job_r.tag(_TAG_ENTRY)
        job_r.tag(_TAG_RANKING_ENTRY)

        logger.info(
            "[entry_exit.tasks] registered tonosama every=%ss tag=%s ranking every minute at :12 tag=%s",
            interval_sec,
            _TAG_TONOSAMA_ENTRY,
            _TAG_RANKING_ENTRY,
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
