# ============================================================
# File   : core/startup/ranking_entry_controller_timeout_patch.py
# Version: V1.1-RANKING-BUILD-TIMEOUT-DISPATCH-PENDING
# ------------------------------------------------------------
# RANKING ENTRY は pending 作成後の entry_controller が timeout しやすい。
# さらに build 側も90秒 timeoutするが、その直前に RANKING PENDING ADD が
# 出ている場合、pending は存在するのに controller が呼ばれない。
#
# 対策:
#   - RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC を既定60秒へ引き上げる
#   - RANKING_ENTRY_BUILD_TIMEOUT_SEC を既定150秒へ引き上げる
#   - build timeout時でも pending が増えていれば controller をdispatchする
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_RANKING_SAFE = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


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
        pass
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", None)
        if isinstance(root, dict):
            for bucket in list(root.values()):
                entries = bucket if isinstance(bucket, (list, tuple, set)) else [bucket]
                for entry in entries:
                    if source_u in _entry_source(entry):
                        total += 1
    except Exception:
        pass
    return int(total)


def _patched_run_ranking_entry_safe() -> int:
    import trading.entry_exit.tasks as tasks

    started_dt = dt.datetime.now()
    started = time.perf_counter()

    with tasks._RANKING_ENTRY_LOCK:
        if tasks._RANKING_ENTRY_COOLDOWN_UNTIL is not None and started_dt < tasks._RANKING_ENTRY_COOLDOWN_UNTIL:
            remain = (tasks._RANKING_ENTRY_COOLDOWN_UNTIL - started_dt).total_seconds()
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=timeout_cooldown remain=%.1fs until=%s timeout_streak=%s", remain, tasks._RANKING_ENTRY_COOLDOWN_UNTIL, tasks._RANKING_ENTRY_TIMEOUT_STREAK)
            return 0
        if tasks._RANKING_ENTRY_RUNNING:
            elapsed = (dt.datetime.now() - tasks._RANKING_ENTRY_STARTED_AT).total_seconds() if tasks._RANKING_ENTRY_STARTED_AT else None
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=previous_still_running started_at=%s elapsed=%s", tasks._RANKING_ENTRY_STARTED_AT, elapsed)
            return 0
        tasks._RANKING_ENTRY_RUNNING = True
        tasks._RANKING_ENTRY_STARTED_AT = started_dt

    try:
        logger.info("[RANKING ENTRY SCHEDULE] fire at=%s patched=v1.1", started_dt.strftime("%Y-%m-%d %H:%M:%S"))
        before_pending = _pending_count_for_source("RANKING")
        build_fn = tasks._resolve_callable("trading.ranking.entry_from_ranking", "run_ranking_entry_pipeline")
        if not callable(build_fn):
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=ranking_entry_pipeline_unavailable")
            return 0

        completed, created_ret = tasks._run_callable_with_timeout(
            build_fn,
            timeout_sec=tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC,
            name="RANKING ENTRY BUILD",
        )
        after_pending = _pending_count_for_source("RANKING")

        if not completed:
            created_by_pending = max(0, after_pending - before_pending)
            logger.warning(
                "[RANKING ENTRY SCHEDULE] build timeout but pending check before=%s after=%s created_by_pending=%s timeout_sec=%.3f elapsed=%.3fs",
                before_pending,
                after_pending,
                created_by_pending,
                tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC,
                time.perf_counter() - started,
            )
            if created_by_pending > 0 or after_pending > 0:
                logger.warning("[RANKING ENTRY SCHEDULE] dispatch controller despite build timeout because pending exists count=%s", after_pending)
                tasks._dispatch_entry_controller(
                    pipeline_source="RANKING",
                    interval=1,
                    timeout_sec=tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC,
                    reason="RANKING ENTRY SCHEDULE",
                )
                with tasks._RANKING_ENTRY_LOCK:
                    tasks._RANKING_ENTRY_TIMEOUT_STREAK = 0
                    tasks._RANKING_ENTRY_COOLDOWN_UNTIL = None
                return int(after_pending)

            with tasks._RANKING_ENTRY_LOCK:
                tasks._RANKING_ENTRY_TIMEOUT_STREAK += 1
                cool_sec = tasks._ranking_entry_cooldown_seconds()
                tasks._RANKING_ENTRY_COOLDOWN_UNTIL = dt.datetime.now() + dt.timedelta(seconds=cool_sec)
            logger.warning(
                "[RANKING ENTRY SCHEDULE] build timeout -> cooldown timeout_sec=%.3f elapsed=%.3fs timeout_streak=%s cooldown_sec=%.1f until=%s",
                tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC,
                time.perf_counter() - started,
                tasks._RANKING_ENTRY_TIMEOUT_STREAK,
                cool_sec,
                tasks._RANKING_ENTRY_COOLDOWN_UNTIL,
            )
            return 0

        with tasks._RANKING_ENTRY_LOCK:
            tasks._RANKING_ENTRY_TIMEOUT_STREAK = 0
            tasks._RANKING_ENTRY_COOLDOWN_UNTIL = None
        created = int(created_ret or 0)
        logger.info("[RANKING ENTRY SCHEDULE] pending build done created=%s before_pending=%s after_pending=%s", created, before_pending, after_pending)
        if created > 0 or after_pending > before_pending:
            tasks._dispatch_entry_controller(
                pipeline_source="RANKING",
                interval=1,
                timeout_sec=tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC,
                reason="RANKING ENTRY SCHEDULE",
            )
        else:
            logger.info("[RANKING ENTRY SCHEDULE] no pending created -> controller dispatch skipped")
        logger.info("[RANKING ENTRY SCHEDULE] done created=%s pending_count=%s elapsed=%.3fs", created, after_pending, time.perf_counter() - started)
        return created
    except Exception:
        logger.exception("[RANKING ENTRY SCHEDULE] failed")
        return 0
    finally:
        with tasks._RANKING_ENTRY_LOCK:
            tasks._RANKING_ENTRY_RUNNING = False
            tasks._RANKING_ENTRY_STARTED_AT = None


def install() -> bool:
    global _INSTALLED, _ORIG_RANKING_SAFE
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", "60")
        os.environ.setdefault("RANKING_ENTRY_BUILD_TIMEOUT_SEC", "150")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "90")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "300")

        import trading.entry_exit.tasks as tasks

        old_controller = float(getattr(tasks, "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 20.0) or 20.0)
        old_build = float(getattr(tasks, "RANKING_ENTRY_BUILD_TIMEOUT_SEC", 90.0) or 90.0)
        new_controller = max(old_controller, _env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 60.0), 60.0)
        new_build = max(old_build, _env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", 150.0), 150.0)

        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = new_controller
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = new_build

        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if callable(cur) and not getattr(cur, "_ranking_timeout_dispatch_patch_v11", False):
            _ORIG_RANKING_SAFE = cur
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v11 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._original = cur  # type: ignore[attr-defined]
            tasks._run_ranking_entry_safe = _patched_run_ranking_entry_safe

        _INSTALLED = True
        logger.warning(
            "[RANKING ENTRY TIMEOUT PATCH] installed v1.1 controller_timeout %.1f->%.1f build_timeout %.1f->%.1f timeout_dispatch_pending=True",
            old_controller,
            new_controller,
            old_build,
            new_build,
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY TIMEOUT PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY TIMEOUT PATCH] auto install failed")

__all__ = ["install"]
