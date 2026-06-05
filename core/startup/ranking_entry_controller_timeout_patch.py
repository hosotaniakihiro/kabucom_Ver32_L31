# ============================================================
# File   : core/startup/ranking_entry_controller_timeout_patch.py
# Version: V1.5-RANKING-NO-EARLY-PENDING-PRUNE
# ------------------------------------------------------------
# RANKING ENTRY は pending 作成後の entry_controller が timeout / filter NG
# になると pending が残り、max_pending=1 のため次回以降のランキング候補生成が
# 全停止しやすい。
#
# V1.4の問題:
#   - dispatch後 0.2秒で pending が残っているだけで
#     RANKING_CONTROLLER_RETURNED_STALE_PENDING として削除していた。
#   - controller / order executor がまだ処理中でも pending が消え、発注まで到達しない。
#
# V1.5対策:
#   - controller_ok=True の場合はデフォルトで即pruneしない。
#   - pruneする場合でも最低滞留秒数を満たしたpendingだけ削除する。
#   - timeout/build時間は毎回runtimeへ再適用し、別patchの上書きに負けにくくする。
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


def _entry_first_seen_ts(entry: Any) -> float:
    try:
        if isinstance(entry, dict):
            for key in (
                "_ranking_pending_first_seen_ts",
                "created_ts",
                "created_at_ts",
                "pending_created_ts",
                "first_seen_ts",
                "ts",
            ):
                v = entry.get(key)
                if v:
                    return float(v)
            # first_seen が無ければ今からの経過0秒として扱い、即pruneを防ぐ。
            now = time.time()
            entry["_ranking_pending_first_seen_ts"] = now
            return now
    except Exception:
        pass
    return time.time()


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


def _pending_symbols_for_source(source: str) -> list[str]:
    source_u = str(source or "").upper()
    symbols: list[str] = []
    try:
        import trading.entry.pending_manager as pm
        iter_entries = getattr(pm, "iter_entries", None)
        if callable(iter_entries):
            for sym, entry in list(iter_entries()):
                if source_u in _entry_source(entry):
                    symbols.append(str(sym))
    except Exception:
        pass
    return sorted(set(symbols))


def _prune_pending_for_source(source: str, reason: str) -> int:
    """
    古い残留pendingだけ掃除する。

    重要:
      dispatch直後のpendingは、entry_controller/order executorがまだ処理中の可能性が高い。
      そのため、最低滞留時間未満のpendingは絶対に削除しない。
    """
    if not _env_bool("RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH", False):
        return 0
    min_age_sec = max(5.0, _env_float("RANKING_ENTRY_STALE_PENDING_MIN_AGE_SEC", 30.0))
    source_u = str(source or "").upper()
    now = time.time()
    try:
        import trading.entry.pending_manager as pm
        prune_entries = getattr(pm, "prune_entries", None)
        if callable(prune_entries):
            def pred(_sym, entry):
                if source_u not in _entry_source(entry):
                    return False
                age = now - _entry_first_seen_ts(entry)
                if age < min_age_sec:
                    logger.warning(
                        "[RANKING ENTRY SCHEDULE] stale prune skipped young pending symbol=%s age=%.1fs min_age=%.1fs reason=%s",
                        _sym,
                        age,
                        min_age_sec,
                        reason,
                    )
                    return False
                return True

            return int(prune_entries(pred, reason=reason) or 0)
    except Exception:
        logger.warning("[RANKING ENTRY SCHEDULE] pending prune failed source=%s reason=%s", source_u, reason, exc_info=True)
    return 0


def _force_runtime_timeouts(tasks) -> None:
    try:
        build = max(float(getattr(tasks, "RANKING_ENTRY_BUILD_TIMEOUT_SEC", 0.0) or 0.0), _env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", 180.0), 180.0)
        controller = max(float(getattr(tasks, "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 0.0) or 0.0), _env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 120.0), 120.0)
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = build
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = controller
    except Exception:
        logger.debug("[RANKING ENTRY TIMEOUT PATCH] force runtime timeouts failed", exc_info=True)


def _dispatch_ranking_controller(tasks, timeout_sec: float) -> bool:
    return bool(tasks._dispatch_entry_controller(
        pipeline_source="RANKING",
        interval=1,
        timeout_sec=timeout_sec,
        reason="RANKING ENTRY SCHEDULE",
    ))


def _dispatch_and_cleanup_ranking(tasks, *, timeout_sec: float, cleanup_reason: str) -> bool:
    before_symbols = _pending_symbols_for_source("RANKING")
    ok = _dispatch_ranking_controller(tasks, timeout_sec)
    # V1.5: controller_ok=Trueなら、pendingが残っていても即stale扱いにしない。
    # order executorが後続で拾う猶予を残す。
    time.sleep(max(0.0, _env_float("RANKING_ENTRY_POST_DISPATCH_GRACE_SEC", 1.0)))
    after_count = _pending_count_for_source("RANKING")
    if after_count > 0:
        if ok and not _env_bool("RANKING_ENTRY_PRUNE_AFTER_SUCCESSFUL_CONTROLLER", False):
            logger.warning(
                "[RANKING ENTRY SCHEDULE] keep ranking pending after controller_ok=True before_symbols=%s after_count=%s reason=%s prune_after_success=False",
                before_symbols,
                after_count,
                cleanup_reason,
            )
            return ok
        removed = _prune_pending_for_source("RANKING", cleanup_reason)
        logger.warning(
            "[RANKING ENTRY SCHEDULE] stale ranking pending cleanup controller_ok=%s before_symbols=%s after_count=%s removed=%s reason=%s",
            ok,
            before_symbols,
            after_count,
            removed,
            cleanup_reason,
        )
    return ok


def _patched_run_ranking_entry_safe() -> int:
    import trading.entry_exit.tasks as tasks

    _force_runtime_timeouts(tasks)
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
        logger.info("[RANKING ENTRY SCHEDULE] fire at=%s patched=v1.5", started_dt.strftime("%Y-%m-%d %H:%M:%S"))
        before_pending = _pending_count_for_source("RANKING")
        if before_pending > 0:
            logger.warning("[RANKING ENTRY SCHEDULE] existing ranking pending detected before build count=%s symbols=%s", before_pending, _pending_symbols_for_source("RANKING"))

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
                _dispatch_and_cleanup_ranking(
                    tasks,
                    timeout_sec=tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC,
                    cleanup_reason="RANKING_BUILD_TIMEOUT_OR_FILTER_NG_STALE",
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

        if created > 0 or after_pending > before_pending or after_pending > 0:
            if created <= 0 and after_pending > 0:
                logger.warning("[RANKING ENTRY SCHEDULE] dispatch existing ranking pending created=0 count=%s symbols=%s", after_pending, _pending_symbols_for_source("RANKING"))
            _dispatch_and_cleanup_ranking(
                tasks,
                timeout_sec=tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC,
                cleanup_reason="RANKING_CONTROLLER_RETURNED_STALE_PENDING",
            )
        else:
            logger.info("[RANKING ENTRY SCHEDULE] no pending created and no ranking pending remains -> controller dispatch skipped")

        final_pending = _pending_count_for_source("RANKING")
        logger.info("[RANKING ENTRY SCHEDULE] done created=%s pending_count=%s final_pending=%s elapsed=%.3fs", created, after_pending, final_pending, time.perf_counter() - started)
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
        old_runtime_budget = os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC")
        old_max_pending = os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN")
        old_prune = os.environ.get("RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH")
        os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = str(max(_env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 150.0), 150.0))
        os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = str(max(int(float(os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN") or 3)), 3))
        os.environ.setdefault("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", "120")
        os.environ.setdefault("RANKING_ENTRY_BUILD_TIMEOUT_SEC", "180")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "90")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "300")
        os.environ.setdefault("RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH", "0")
        os.environ.setdefault("RANKING_ENTRY_PRUNE_AFTER_SUCCESSFUL_CONTROLLER", "0")
        os.environ.setdefault("RANKING_ENTRY_STALE_PENDING_MIN_AGE_SEC", "30")
        os.environ.setdefault("RANKING_ENTRY_POST_DISPATCH_GRACE_SEC", "1.0")

        import trading.entry_exit.tasks as tasks

        old_controller = float(getattr(tasks, "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 20.0) or 20.0)
        old_build = float(getattr(tasks, "RANKING_ENTRY_BUILD_TIMEOUT_SEC", 90.0) or 90.0)
        _force_runtime_timeouts(tasks)
        new_controller = float(getattr(tasks, "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 120.0) or 120.0)
        new_build = float(getattr(tasks, "RANKING_ENTRY_BUILD_TIMEOUT_SEC", 180.0) or 180.0)

        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if callable(cur) and not getattr(cur, "_ranking_timeout_dispatch_patch_v15", False):
            _ORIG_RANKING_SAFE = cur
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v14 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v15 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._original = cur  # type: ignore[attr-defined]
            tasks._run_ranking_entry_safe = _patched_run_ranking_entry_safe

        _INSTALLED = True
        logger.warning(
            "[RANKING ENTRY TIMEOUT PATCH] installed v1.5 controller_timeout %.1f->%.1f build_timeout %.1f->%.1f runtime_budget old=%s new=%s max_pending old=%s new=%s stale_prune old=%s new=%s no_early_prune=True",
            old_controller,
            new_controller,
            old_build,
            new_build,
            old_runtime_budget,
            os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            old_max_pending,
            os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
            old_prune,
            os.environ.get("RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH"),
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