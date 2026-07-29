# ============================================================
# File   : trading/entry_exit/tasks.py
# Version: Ver2.5-INLINE-RANKING-ENTRY-SAFE-CHAIN
# ------------------------------------------------------------
# 【目的】
#   entry/exit scheduler tasks.
#
# Ver2.5:
#   - core/startup/ranking_entry_controller_timeout_patch.py (V1.9) /
#     ranking_entry_hard_timeout_patch.py (V3) /
#     ranking_entry_market_hours_skip_patch.py (V1.4) /
#     ranking_stuck_pending_prune_patch.py (V7) が同じ
#     _run_ranking_entry_safe を非決定的な順序で奪い合っていた連鎖を、
#     本文へ1つの決定論的な関数として統合した。
#   - タイムアウト方針は V3/V7 が独立に文書化した緩和値
#     (build/controller<=30s, runtime budget 25s, 外側ハード35-55s) を採用し、
#     V1.9 の18/12/15秒ハードキャップ (created=0 障害の原因) は撤去した。
#
# Ver2.4 Fix:
#   - TONOSAMA実行前の fresh push summary wait が get_push_merged_summary(1)
#     だけを見ていたため、merged cache が一瞬空の時に
#       latest=None rows=0 -> skip this cycle
#     となり、DB/summary_historyには最新データがあるのにTonosamaが起動しない。
#   - fresh判定の取得元を push merged -> merged -> summary_history ->
#     tonosama summary_loader の順でフォールバック。
#   - それでも latest を取れない場合は、Tonosama本体の安全ガードに任せるため
#     既定で fail-open して起動する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from pathlib import Path
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

# 旧 core/startup/ranking_stuck_pending_prune_patch.py の overlap-guard 用ロック。
# _RANKING_ENTRY_LOCK (cooldown/running flag の短時間保護用) とは別に、
# ビルド〜controller dispatchまでの「重い一連の処理」全体の多重実行を防ぐ。
_RANKING_ENTRY_RUN_LOCK = threading.Lock()
_RANKING_ENTRY_RUN_STARTED_AT = 0.0
_RANKING_ENTRY_RUN_SEQ = 0
_RANKING_ENTRY_COMPANION_PATCHED = False


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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


TONOSAMA_ENTRY_TIMEOUT_SEC = max(30.0, _env_float("TONOSAMA_ENTRY_TIMEOUT_SEC", 30.0))
TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC = max(8.0, _env_float("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", 8.0))
TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC = _env_float("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC", 45.0)
TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC = _env_float("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", 180.0)

# NOTE: build/controller timeoutの既定値は 30.0/30.0秒 (旧 90.0/20.0秒から変更)。
# 旧 core/startup/ranking_entry_controller_timeout_patch.py (V1.9) は18/12/15秒まで
# 締め付けていたが、旧 ranking_entry_hard_timeout_patch.py (V3) / ranking_stuck_pending_prune_patch.py (V7)
# の両方が独立に「締めすぎて候補はあるのにpending追加がタイムアウトで弾かれ created=0 になる」障害を
# 報告し、25-30秒への緩和で修正していた。本文化にあたり、その緩和値を正として採用する。
RANKING_ENTRY_BUILD_TIMEOUT_SEC = _env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", 30.0)
RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = _env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 30.0)
RANKING_ENTRY_RUNTIME_BUDGET_SEC = _env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 25.0)
# 外側のハード安全弁。ビルド+controller dispatchの合計がここを超えたら、workerはdaemonのまま
# 走らせ続け、schedulerには一旦0を返してスロットを解放する。
RANKING_ENTRY_HARD_TIMEOUT_SEC = max(1.0, min(_env_float("RANKING_ENTRY_HARD_TIMEOUT_SEC", 35.0), 55.0))
RANKING_ENTRY_MAX_PENDING_PER_RUN = max(1, _env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", 4))
RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC = _env_float("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", 90.0)
RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC = _env_float("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", 300.0)


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


def _entry_first_seen_ts(entry: Any) -> float:
    try:
        if isinstance(entry, dict):
            for key in ("_ranking_pending_first_seen_ts", "created_ts", "created_at_ts", "pending_created_ts", "first_seen_ts", "ts"):
                v = entry.get(key)
                if v:
                    return float(v)
            now = time.time()
            entry["_ranking_pending_first_seen_ts"] = now
            return now
    except Exception:
        pass
    return time.time()


def _prune_pending_for_source(source: str, reason: str) -> int:
    if not _env_bool("RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH", True):
        return 0
    min_age_sec = max(3.0, _env_float("RANKING_ENTRY_STALE_PENDING_MIN_AGE_SEC", 10.0))
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
                    logger.warning("[RANKING ENTRY SCHEDULE] stale prune skipped young pending symbol=%s age=%.1fs min_age=%.1fs reason=%s", _sym, age, min_age_sec, reason)
                    return False
                return True
            return int(prune_entries(pred, reason=reason) or 0)
    except Exception:
        logger.warning("[RANKING ENTRY SCHEDULE] pending prune failed source=%s reason=%s", source_u, reason, exc_info=True)
    return 0


def _dispatch_and_cleanup_ranking(*, timeout_sec: float, cleanup_reason: str) -> bool:
    before_symbols = _pending_symbols_for_source("RANKING")
    ok = _dispatch_entry_controller(pipeline_source="RANKING", interval=1, timeout_sec=timeout_sec, reason="RANKING ENTRY SCHEDULE")
    time.sleep(max(0.0, _env_float("RANKING_ENTRY_POST_DISPATCH_GRACE_SEC", 0.1)))
    after_count = _pending_count_for_source("RANKING")
    if after_count > 0:
        removed = _prune_pending_for_source("RANKING", cleanup_reason)
        logger.warning("[RANKING ENTRY SCHEDULE] ranking pending after controller controller_ok=%s before_symbols=%s after_count=%s removed=%s reason=%s", ok, before_symbols, after_count, removed, cleanup_reason)
    return ok


def _ranking_entry_pending_score(entry: Any) -> float:
    try:
        if isinstance(entry, dict):
            return float(entry.get("score") or entry.get("ranking_score") or entry.get("pending_score") or 0.0)
    except Exception:
        pass
    return 0.0


def _mark_and_prune_stuck_ranking_pending(reason: str = "RANKING_STUCK_PENDING_RETRY_OR_AGE") -> int:
    """旧 core/startup/ranking_stuck_pending_prune_patch.py (V7) のstuck pending判定・prune。"""
    max_retry = max(1, _env_int("RANKING_STUCK_PENDING_MAX_CONTROLLER_RETRY", 3))
    min_age_sec = max(5.0, _env_float("RANKING_STUCK_PENDING_MIN_AGE_SEC", 30.0))
    max_age_sec = max(min_age_sec, _env_float("RANKING_STUCK_PENDING_MAX_AGE_SEC", 120.0))
    now = time.time()

    try:
        import trading.entry.pending_manager as pm
        it = getattr(pm, "iter_entries", None)
        prune = getattr(pm, "prune_entries", None)
        if not callable(it) or not callable(prune):
            return 0

        for sym, entry in list(it()):
            if not isinstance(entry, dict) or "RANKING" not in _entry_source(entry):
                continue
            first = entry.get("_ranking_pending_first_seen_ts")
            if not first:
                entry["_ranking_pending_first_seen_ts"] = now
                first = now
            entry["_ranking_controller_retry_count"] = int(float(entry.get("_ranking_controller_retry_count") or 0)) + 1
            entry["_ranking_last_controller_retry_ts"] = now
            logger.info(
                "[RANKING ENTRY SCHEDULE] stuck pending mark symbol=%s retry=%s age=%.1fs score=%.4f min_age=%.1fs max_age=%.1fs",
                sym,
                entry.get("_ranking_controller_retry_count"),
                now - float(first),
                _ranking_entry_pending_score(entry),
                min_age_sec,
                max_age_sec,
            )

        def pred(sym: str, entry: dict) -> bool:
            if not isinstance(entry, dict) or "RANKING" not in _entry_source(entry):
                return False
            retry = int(float(entry.get("_ranking_controller_retry_count") or 0))
            first = float(entry.get("_ranking_pending_first_seen_ts") or now)
            age = now - first
            if age < min_age_sec:
                return False
            if age >= max_age_sec:
                return True
            if retry >= max_retry:
                return True
            return False

        removed = int(prune(pred, reason=reason))
        if removed:
            logger.warning(
                "[RANKING ENTRY SCHEDULE] stuck pending pruned removed=%s reason=%s max_retry=%s min_age=%.1fs max_age=%.1fs",
                removed,
                reason,
                max_retry,
                min_age_sec,
                max_age_sec,
            )
        return removed
    except Exception:
        logger.exception("[RANKING ENTRY SCHEDULE] stuck pending prune failed")
        return 0


def _clear_ranking_runtime_overlap_if_stale() -> bool:
    """旧 ranking_stuck_pending_prune_patch.py の_clear_runtime_overlap_if_stale。

    scheduler の previous_still_running 化を避けるため、_RANKING_ENTRY_RUN_LOCK を
    握ったまま古くなった実行を検知したら、次サイクルを止める代わりにstuck pendingを掃除する。
    """
    global _RANKING_ENTRY_RUN_STARTED_AT
    if not _RANKING_ENTRY_RUN_LOCK.locked():
        return False
    stale_sec = max(20.0, _env_float("RANKING_ENTRY_RUNTIME_STALE_SEC", 35.0))
    age = time.time() - float(_RANKING_ENTRY_RUN_STARTED_AT or 0.0)
    if age < stale_sec:
        return False
    logger.warning(
        "[RANKING ENTRY SCHEDULE] previous ranking run still active age=%.1fs >= %.1fs; "
        "skip this cycle and prune stale pending instead of starting another heavy run",
        age,
        stale_sec,
    )
    _mark_and_prune_stuck_ranking_pending(reason="RANKING_ENTRY_RUNTIME_STALE_SKIP")
    return True


def _ranking_entry_in_session(now: Optional[dt.datetime] = None) -> bool:
    """旧 ranking_entry_market_hours_skip_patch.py の_in_session。"""
    now = now or dt.datetime.now()
    t = now.time()
    return (dt.time(9, 0) <= t <= dt.time(11, 30)) or (dt.time(12, 30) <= t <= dt.time(15, 30))


def _clear_ranking_task_running_if_stale(*, force: bool = False) -> bool:
    """旧 ranking_entry_market_hours_skip_patch.py の_clear_task_running_if_stale (RANKING専用)。"""
    global _RANKING_ENTRY_RUNNING, _RANKING_ENTRY_STARTED_AT, _RANKING_ENTRY_COOLDOWN_UNTIL
    timeout = _env_float("RANKING_ENTRY_TASK_STALE_SEC", _env_float("RANKING_ENTRY_SCHEDULER_STALE_SEC", 30.0))
    try:
        with _RANKING_ENTRY_LOCK:
            running = bool(_RANKING_ENTRY_RUNNING)
            started_at = _RANKING_ENTRY_STARTED_AT
            elapsed = None
            if isinstance(started_at, dt.datetime):
                elapsed = max(0.0, (dt.datetime.now() - started_at).total_seconds())
            should_clear = bool(force)
            if running and elapsed is not None and elapsed >= timeout:
                should_clear = True
            if not should_clear:
                return False
            _RANKING_ENTRY_RUNNING = False
            _RANKING_ENTRY_STARTED_AT = None
            _RANKING_ENTRY_COOLDOWN_UNTIL = None
            logger.warning(
                "[RANKING ENTRY SCHEDULE] cleared stale task-running flag elapsed=%s timeout=%.3fs force=%s",
                None if elapsed is None else round(float(elapsed), 3),
                timeout,
                force,
            )
            return True
    except Exception:
        logger.exception("[RANKING ENTRY SCHEDULE] task stale clear failed")
        return False


def _ranking_entry_operation_mode() -> str:
    try:
        return str(os.getenv("AUTOSTOCK_MAIN_OPERATION_MODE", "full") or "full").strip().lower()
    except Exception:
        return "full"


def _is_main_py_process() -> bool:
    try:
        return Path(sys.argv[0]).name.lower() == "main.py"
    except Exception:
        return False


def _main_skip_ranking_entry() -> bool:
    """旧 ranking_stuck_pending_prune_patch.py の_main_skip_ranking_entry。

    entry_only 安全モード時だけ main.py の ranking entry を止める。
    """
    if not _is_main_py_process():
        return False
    if os.getenv("AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY") is not None:
        return _env_bool("AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY", False)
    return _ranking_entry_operation_mode() not in {"full", "all"} and not _env_bool("AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY", False)


def _install_ranking_entry_companion_patches() -> bool:
    """旧 ranking_entry_market_hours_skip_patch.py / ranking_stuck_pending_prune_patch.py が
    連鎖installしていたcompanion patchを1回だけ呼ぶ。これらは本文化された_run_ranking_entry_safe
    には依存しない独立の機能のため、本体削除後もinstallされ続けるようここへ移設する。
    """
    global _RANKING_ENTRY_COMPANION_PATCHED
    if _RANKING_ENTRY_COMPANION_PATCHED:
        return True
    ok_any = False
    for module_name in (
        "core.startup.kabu_api_token_runtime_patch",
        "core.startup.ranking_entry_push_fallback_patch",
        "core.startup.ranking_entry_min_pending_on_timeout_patch",
    ):
        try:
            import importlib
            mod = importlib.import_module(module_name)
            fn = getattr(mod, "install", None)
            ok = bool(fn()) if callable(fn) else False
            ok_any = ok_any or ok
            logger.info("[RANKING ENTRY SCHEDULE] companion patch %s installed=%s", module_name, ok)
        except Exception:
            logger.debug("[RANKING ENTRY SCHEDULE] companion patch %s skipped", module_name, exc_info=True)
    _RANKING_ENTRY_COMPANION_PATCHED = True
    return ok_any


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


def _run_callable_with_timeout_thread(fn: Callable[..., Any], *, timeout_sec: float, name: str, args: tuple[Any, ...] = (), kwargs: Optional[dict[str, Any]] = None) -> tuple[bool, Any, Optional[threading.Thread]]:
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


def _latest_from_df(df: Any, *, source_name: str) -> tuple[float | None, dt.datetime | None, int, str]:
    try:
        import pandas as pd
        if df is None or not isinstance(df, pd.DataFrame):
            return None, None, 0, source_name
        rows = int(len(df))
        if df.empty:
            return None, None, rows, source_name
        col = None
        for c in ("datetime", "end_time", "start_time", "time", "snapshot_time"):
            if c in df.columns:
                col = c
                break
        if not col:
            return None, None, rows, source_name
        s = pd.to_datetime(df[col], errors="coerce").dropna()
        if s.empty:
            return None, None, rows, source_name
        latest = s.max().to_pydatetime().replace(tzinfo=None)
        age = (dt.datetime.now() - latest).total_seconds()
        return float(age), latest, rows, f"{source_name}:{col}"
    except Exception:
        logger.debug("[TONOSAMA ENTRY SCHEDULE] latest_from_df failed source=%s", source_name, exc_info=True)
        return None, None, 0, source_name


def _latest_push_summary_age_sec() -> tuple[float | None, dt.datetime | None, int, str]:
    """Return freshest 1m PUSH summary age from multiple caches.

    Important: get_push_merged_summary(1) can be empty during a brief publish gap.
    Do not skip Tonosama solely because that single cache is empty.
    """
    candidates: list[tuple[str, Any]] = []
    try:
        import core.global_context.context as ctx
        for name, call in [
            ("get_push_merged_summary", lambda: ctx.get_push_merged_summary(1)),
            ("get_merged_summary_push", lambda: ctx.get_merged_summary(1, source="push")),
            ("get_summary_history_push", lambda: ctx.get_summary_history(1, source="push")),
        ]:
            try:
                fn_df = call()
                candidates.append((name, fn_df))
            except Exception:
                logger.debug("[TONOSAMA ENTRY SCHEDULE] provider failed %s", name, exc_info=True)
    except Exception:
        logger.debug("[TONOSAMA ENTRY SCHEDULE] core.global_context.context import failed", exc_info=True)

    try:
        from trading.entry.tonosama.summary_loader import load_merged_summary
        candidates.append(("tonosama.summary_loader.load_merged_summary", load_merged_summary(1)))
    except Exception:
        logger.debug("[TONOSAMA ENTRY SCHEDULE] tonosama summary_loader fallback failed", exc_info=True)

    best = (None, None, 0, "none")
    for name, df in candidates:
        age, latest, rows, src = _latest_from_df(df, source_name=name)
        if latest is None:
            if rows > best[2]:
                best = (age, latest, rows, src)
            continue
        if best[1] is None or latest > best[1]:
            best = (age, latest, rows, src)
    return best


def _wait_fresh_push_summary_before_tonosama() -> bool:
    if not _env_bool("TONOSAMA_WAIT_FRESH_PUSH_SUMMARY", True):
        return True
    max_age = max(30.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_MAX_AGE_SEC", 180.0))
    wait_sec = max(0.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC", 15.0))
    poll = max(0.25, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_POLL_SEC", 1.0))
    fail_open_empty = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", True)
    deadline = time.perf_counter() + wait_sec
    last_age = None
    last_dt = None
    last_rows = 0
    last_src = "none"

    while True:
        age, latest, rows, src = _latest_push_summary_age_sec()
        last_age, last_dt, last_rows, last_src = age, latest, rows, src
        if age is not None and age <= max_age:
            if wait_sec > 0:
                logger.info("[TONOSAMA ENTRY SCHEDULE] fresh push summary ok latest=%s age=%.1fs rows=%s max_age=%.1fs source=%s", latest, age, rows, max_age, src)
            return True
        if latest is None and fail_open_empty:
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary unavailable latest=None rows=%s source=%s -> fail-open to Tonosama body; body has its own stale guard",
                rows,
                src,
            )
            return True
        if time.perf_counter() >= deadline:
            if last_dt is None and fail_open_empty:
                logger.warning(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired latest=None rows=%s source=%s -> fail-open to Tonosama body",
                    last_rows,
                    last_src,
                )
                return True
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired latest=%s age=%s rows=%s max_age=%.1fs wait_sec=%.1fs source=%s -> skip this cycle",
                last_dt, None if last_age is None else round(last_age, 1), last_rows, max_age, wait_sec, last_src,
            )
            return False
        time.sleep(poll)


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
        if not _wait_fresh_push_summary_before_tonosama():
            return 0
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
                TONOSAMA_ENTRY_TIMEOUT_SEC, time.perf_counter() - started, after_pending, _TONOSAMA_ENTRY_TIMEOUT_STREAK, cool_sec, _TONOSAMA_ENTRY_COOLDOWN_UNTIL, _env_bool("TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING", False),
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


def _run_ranking_entry_safe_body(seq: int) -> int:
    """実際のビルド〜controller dispatchロジック本体。

    _RANKING_ENTRY_RUN_LOCK 取得後、ハードタイムアウトのworkerスレッド内で呼ばれる。
    """
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
            stale_sec = _env_float("RANKING_ENTRY_RUNNING_STALE_RESET_SEC", 45.0)
            if elapsed is not None and elapsed > stale_sec:
                logger.warning("[RANKING ENTRY SCHEDULE] stale running flag reset elapsed=%.1fs stale_sec=%.1fs started_at=%s", elapsed, stale_sec, _RANKING_ENTRY_STARTED_AT)
                _RANKING_ENTRY_RUNNING = False
                _RANKING_ENTRY_STARTED_AT = None
            else:
                logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=previous_still_running started_at=%s elapsed=%s", _RANKING_ENTRY_STARTED_AT, elapsed)
                return 0
        _RANKING_ENTRY_RUNNING = True
        _RANKING_ENTRY_STARTED_AT = started_dt
    try:
        logger.info("[RANKING ENTRY SCHEDULE] fire at=%s seq=%s", started_dt.strftime("%Y-%m-%d %H:%M:%S"), seq)

        before_pending = _pending_count_for_source("RANKING")
        if before_pending > 0:
            pruned = _mark_and_prune_stuck_ranking_pending()
            if pruned:
                logger.warning("[RANKING ENTRY SCHEDULE] pre-build pruned=%s before=%s after=%s", pruned, before_pending, _pending_count_for_source("RANKING"))
            before_pending = _pending_count_for_source("RANKING")

        if before_pending > 0:
            logger.warning("[RANKING ENTRY SCHEDULE] existing ranking pending detected before build count=%s symbols=%s", before_pending, _pending_symbols_for_source("RANKING"))
            # 先に既存pendingを捌く。ビルドを重ねて詰まらせない。
            _dispatch_and_cleanup_ranking(timeout_sec=RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC, cleanup_reason="RANKING_EXISTING_PENDING_FIRST")
            if _pending_count_for_source("RANKING") >= RANKING_ENTRY_MAX_PENDING_PER_RUN:
                logger.warning("[RANKING ENTRY SCHEDULE] build skipped because pending remains count=%s", _pending_count_for_source("RANKING"))
                return 0

        build_fn = _resolve_callable("trading.ranking.entry_from_ranking", "run_ranking_entry_pipeline")
        if not callable(build_fn):
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=ranking_entry_pipeline_unavailable")
            return 0
        completed, created_ret = _run_callable_with_timeout(build_fn, timeout_sec=RANKING_ENTRY_BUILD_TIMEOUT_SEC, name="RANKING ENTRY BUILD")
        after_pending = _pending_count_for_source("RANKING")
        if not completed:
            created_by_pending = max(0, after_pending - before_pending)
            logger.warning("[RANKING ENTRY SCHEDULE] build timeout but pending check before=%s after=%s created_by_pending=%s timeout_sec=%.3f elapsed=%.3fs", before_pending, after_pending, created_by_pending, RANKING_ENTRY_BUILD_TIMEOUT_SEC, time.perf_counter() - started)
            if created_by_pending > 0 or after_pending > 0:
                logger.warning("[RANKING ENTRY SCHEDULE] dispatch controller despite build timeout because pending exists count=%s", after_pending)
                _dispatch_and_cleanup_ranking(timeout_sec=RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC, cleanup_reason="RANKING_BUILD_TIMEOUT_OR_FILTER_NG_STALE")
                with _RANKING_ENTRY_LOCK:
                    _RANKING_ENTRY_TIMEOUT_STREAK = 0
                    _RANKING_ENTRY_COOLDOWN_UNTIL = None
                return int(after_pending)
            with _RANKING_ENTRY_LOCK:
                _RANKING_ENTRY_TIMEOUT_STREAK += 1
                cool_sec = _ranking_entry_cooldown_seconds()
                _RANKING_ENTRY_COOLDOWN_UNTIL = dt.datetime.now() + dt.timedelta(seconds=cool_sec)
            logger.warning("[RANKING ENTRY SCHEDULE] build timeout -> cooldown timeout_sec=%.3f elapsed=%.3fs timeout_streak=%s cooldown_sec=%.1f until=%s", RANKING_ENTRY_BUILD_TIMEOUT_SEC, time.perf_counter() - started, _RANKING_ENTRY_TIMEOUT_STREAK, cool_sec, _RANKING_ENTRY_COOLDOWN_UNTIL)
            return 0

        with _RANKING_ENTRY_LOCK:
            _RANKING_ENTRY_TIMEOUT_STREAK = 0
            _RANKING_ENTRY_COOLDOWN_UNTIL = None
        created = int(created_ret or 0)
        logger.info("[RANKING ENTRY SCHEDULE] pending build done created=%s before_pending=%s after_pending=%s", created, before_pending, after_pending)
        if created > 0 or after_pending > before_pending or after_pending > 0:
            if created <= 0 and after_pending > 0:
                logger.warning("[RANKING ENTRY SCHEDULE] dispatch existing ranking pending created=0 count=%s symbols=%s", after_pending, _pending_symbols_for_source("RANKING"))
            _dispatch_and_cleanup_ranking(timeout_sec=RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC, cleanup_reason="RANKING_CONTROLLER_RETURNED_STALE_PENDING")
        else:
            logger.info("[RANKING ENTRY SCHEDULE] no pending created and no ranking pending remains -> controller dispatch skipped")
        final_pending = _pending_count_for_source("RANKING")
        logger.info("[RANKING ENTRY SCHEDULE] done created=%s pending_count=%s final_pending=%s elapsed=%.3fs seq=%s", created, after_pending, final_pending, time.perf_counter() - started, seq)
        return created
    except Exception:
        logger.exception("[RANKING ENTRY SCHEDULE] failed seq=%s", seq)
        return 0
    finally:
        with _RANKING_ENTRY_LOCK:
            _RANKING_ENTRY_RUNNING = False
            _RANKING_ENTRY_STARTED_AT = None


def _run_ranking_entry_safe() -> int:
    """RANKING entryのスケジュールタスク本体（本文化統合版）。

    外側から内側への処理順序:
      1. 市場時間外スキップ (旧 ranking_entry_market_hours_skip_patch.py)
      2. RANKING task-levelのstale running clear (同上)
      3. main.py entry_onlyモードでのskipゲート (旧 ranking_stuck_pending_prune_patch.py)
      4. companion patchのインストール (kabu_api_token_runtime_patch 等、1回だけ)
      5. _RANKING_ENTRY_RUN_LOCK (非ブロッキング) + stale-overlap時のprune (旧 ranking_stuck_pending_prune_patch.py)
      6. 外側ハードタイムアウト (旧 ranking_entry_hard_timeout_patch.py, 35-55秒) でworkerスレッドを被せる
      7. worker内部: cooldown/実行中チェック -> 既存pending dispatch+cleanup -> stuck pending prune
         -> build (floor 30秒) -> controller dispatch+cleanup -> cooldown/streak更新

    タイムアウト方針は旧 ranking_entry_hard_timeout_patch.py (V3) / ranking_stuck_pending_prune_patch.py (V7)
    が独立に文書化した緩和値 (build/controller<=30s, runtime budget 25s, 外側35-55s) を正として採用する。
    旧 ranking_entry_controller_timeout_patch.py (V1.9) の18/12/15秒ハードキャップは、
    上記2パッチが対処した「created=0になる」障害の原因だったため採用しない。
    """
    global _RANKING_ENTRY_RUN_STARTED_AT, _RANKING_ENTRY_RUN_SEQ

    now = dt.datetime.now()
    if not _ranking_entry_in_session(now):
        logger.warning("[RANKING ENTRY SCHEDULE] skip outside session now=%s", now.strftime("%Y-%m-%d %H:%M:%S"))
        return 0

    _clear_ranking_task_running_if_stale()

    if _main_skip_ranking_entry():
        logger.warning(
            "[RANKING ENTRY SCHEDULE] main.py skip ranking entry job mode=%s. "
            "Set AUTOSTOCK_MAIN_OPERATION_MODE=full or AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY=0 to restore.",
            _ranking_entry_operation_mode(),
        )
        return 0

    _install_ranking_entry_companion_patches()

    if not _RANKING_ENTRY_RUN_LOCK.acquire(blocking=False):
        _clear_ranking_runtime_overlap_if_stale()
        return 0

    _RANKING_ENTRY_RUN_SEQ += 1
    seq = _RANKING_ENTRY_RUN_SEQ
    _RANKING_ENTRY_RUN_STARTED_AT = time.time()

    def _inner() -> int:
        global _RANKING_ENTRY_RUN_STARTED_AT
        try:
            return _run_ranking_entry_safe_body(seq)
        finally:
            _RANKING_ENTRY_RUN_STARTED_AT = 0.0
            try:
                _RANKING_ENTRY_RUN_LOCK.release()
            except RuntimeError:
                pass

    completed, ret = _run_callable_with_timeout(_inner, timeout_sec=RANKING_ENTRY_HARD_TIMEOUT_SEC, name="RANKING ENTRY HARD TIMEOUT")
    if not completed:
        logger.warning(
            "[RANKING ENTRY SCHEDULE] hard timeout seq=%s timeout_sec=%.1fs; return 0 to release scheduler slot, worker continues in daemon thread",
            seq,
            RANKING_ENTRY_HARD_TIMEOUT_SEC,
        )
        try:
            _mark_and_prune_stuck_ranking_pending(reason="RANKING_ENTRY_HARD_TIMEOUT")
        except Exception:
            logger.debug("[RANKING ENTRY SCHEDULE] prune on hard timeout failed", exc_info=True)
        return 0
    return int(ret or 0)


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
            "[entry_exit.tasks] registered tonosama every=%ss tag=%s build_timeout=%.1fs controller_timeout=%.1fs timeout_cooldown=%.1f-%0.1fs wait_fresh_summary=%s fail_open_empty=%s dispatch_timeout_pending=%s ranking every=%smin at :12 tag=%s build_timeout=%.1fs controller_timeout=%.1fs cooldown=%.1f-%0.1fs pending_count_global=True",
            interval_sec, _TAG_TONOSAMA_ENTRY, TONOSAMA_ENTRY_TIMEOUT_SEC, TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC,
            TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC, TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC,
            _env_bool("TONOSAMA_WAIT_FRESH_PUSH_SUMMARY", True),
            _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", True),
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


__all__ = ["register_entry_exit_tasks", "_run_tonosama_entry_safe", "_run_ranking_entry_safe"]
