from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False
_RUN_LOCK = threading.Lock()
_RUN_STARTED_AT = 0.0
_RUN_SEQ = 0
_SUMMARY_FALLBACK_PATCHED = False
_MIN_PENDING_PATCHED = False


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _cap_env_float(name: str, hard_cap: float, default: float, *, floor: float = 1.0) -> float:
    cur = _env_float(name, default)
    val = max(float(floor), min(float(cur), float(hard_cap)))
    os.environ[name] = str(val)
    return val


def _cap_env_int(name: str, hard_cap: int, default: int, *, floor: int = 1) -> int:
    cur = _env_int(name, default)
    val = max(int(floor), min(int(cur), int(hard_cap)))
    os.environ[name] = str(val)
    return val


def _force_ranking_runtime_caps() -> dict[str, Any]:
    """Keep ranking_entry bounded without overriding fast-budget rescue too tightly.

    V6 forced runtime/build/controller to 15/18/12s. That was too short: logs showed
    candidates were found but pending_add was skipped by timeout, leaving created=0.
    V7 aligns the cap with ranking_entry_fast_budget_override defaults: 25/30/30s.
    """
    runtime_default = _env_float("RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC", 25.0)
    build_default = _env_float("RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC", 30.0)
    controller_default = _env_float("RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC", 30.0)
    caps = {
        "runtime_budget": _cap_env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 25.0, runtime_default, floor=15.0),
        "runtime_warn": _cap_env_float("RANKING_ENTRY_RUNTIME_WARN_SEC", 25.0, runtime_default, floor=15.0),
        "runtime_stale": _cap_env_float("RANKING_ENTRY_RUNTIME_STALE_SEC", 35.0, 35.0, floor=20.0),
        "build_timeout": _cap_env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", 30.0, build_default, floor=18.0),
        "controller_timeout": _cap_env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 30.0, controller_default, floor=12.0),
        "max_pending": _cap_env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", 4, 4, floor=3),
        "prefilter_rows": _cap_env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 32, 24, floor=12),
        "max_symbols": _cap_env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", 32, 24, floor=12),
        "source_rows": _cap_env_int("RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS", 300, 300, floor=120),
    }
    return caps


def _operation_mode() -> str:
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
    """entry_only 安全モード時だけ main.py の ranking entry を止める。"""
    if not _is_main_py_process():
        return False
    if os.getenv("AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY") is not None:
        return _env_bool("AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY", False)
    return _operation_mode() not in {"full", "all"} and not _env_bool("AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY", False)


def _source(entry: Any) -> str:
    try:
        if isinstance(entry, dict):
            return str(entry.get("source") or entry.get("entry_type") or entry.get("pipeline_source") or "").upper()
        return str(getattr(entry, "source", "") or getattr(entry, "entry_type", "") or getattr(entry, "pipeline_source", "")).upper()
    except Exception:
        return ""


def _score(entry: Any) -> float:
    try:
        if isinstance(entry, dict):
            return float(entry.get("score") or entry.get("ranking_score") or entry.get("pending_score") or 0.0)
    except Exception:
        pass
    return 0.0


def _pending_count() -> int:
    total = 0
    try:
        import trading.entry.pending_manager as pm
        it = getattr(pm, "iter_entries", None)
        if callable(it):
            for _sym, e in list(it()):
                if "RANKING" in _source(e):
                    total += 1
            return total
    except Exception:
        pass
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", None)
        if isinstance(root, dict):
            for bucket in root.values():
                entries = bucket if isinstance(bucket, (list, tuple, set)) else [bucket]
                for e in entries:
                    if "RANKING" in _source(e):
                        total += 1
    except Exception:
        pass
    return total


def _install_min_pending_patch() -> bool:
    """Ensure candidate>=1 is not lost just before pending_add timeout."""
    global _MIN_PENDING_PATCHED
    try:
        from core.startup import ranking_entry_min_pending_on_timeout_patch as mp
        ok = bool(mp.install())
        _MIN_PENDING_PATCHED = ok
        logger.warning("[RANKING STUCK PENDING] min pending timeout rescue installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[RANKING STUCK PENDING] min pending timeout rescue install failed")
        return False


def _mark_and_prune_stuck_ranking_pending(reason: str = "RANKING_STUCK_PENDING_RETRY_OR_AGE") -> int:
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
            if not isinstance(entry, dict) or "RANKING" not in _source(entry):
                continue
            first = entry.get("_ranking_pending_first_seen_ts")
            if not first:
                entry["_ranking_pending_first_seen_ts"] = now
                first = now
            entry["_ranking_controller_retry_count"] = int(float(entry.get("_ranking_controller_retry_count") or 0)) + 1
            entry["_ranking_last_controller_retry_ts"] = now
            logger.info(
                "[RANKING STUCK PENDING] mark symbol=%s retry=%s age=%.1fs score=%.4f min_age=%.1fs max_age=%.1fs",
                sym,
                entry.get("_ranking_controller_retry_count"),
                now - float(first),
                _score(entry),
                min_age_sec,
                max_age_sec,
            )

        def pred(sym: str, entry: dict) -> bool:
            if not isinstance(entry, dict) or "RANKING" not in _source(entry):
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
                "[RANKING STUCK PENDING] pruned removed=%s reason=%s max_retry=%s min_age=%.1fs max_age=%.1fs",
                removed,
                reason,
                max_retry,
                min_age_sec,
                max_age_sec,
            )
        return removed
    except Exception:
        logger.exception("[RANKING STUCK PENDING] prune failed")
        return 0


def _clear_runtime_overlap_if_stale() -> bool:
    """schedulerのprevious_still_running化を避けるため、古い実行中マークを自前で解放する。"""
    global _RUN_STARTED_AT
    if not _RUN_LOCK.locked():
        return False
    stale_sec = max(20.0, _env_float("RANKING_ENTRY_RUNTIME_STALE_SEC", 35.0))
    age = time.time() - float(_RUN_STARTED_AT or 0.0)
    if age < stale_sec:
        return False

    logger.warning(
        "[RANKING STUCK PENDING] previous ranking run still active age=%.1fs >= %.1fs; "
        "skip this cycle and prune stale pending instead of starting another heavy run",
        age,
        stale_sec,
    )
    _mark_and_prune_stuck_ranking_pending(reason="RANKING_ENTRY_RUNTIME_STALE_SKIP")
    return True


def _patch_summary_fallback_loader() -> bool:
    """PUSH表示/判定で source=push の実データを recovery_yahoo より優先させる。"""
    global _SUMMARY_FALLBACK_PATCHED
    try:
        import pandas as pd
        import scheduler_jobs.summary.fallback_loader as fl

        cur = getattr(fl, "filter_push_like_rows", None)
        if getattr(cur, "_real_push_priority_v1", False):
            _SUMMARY_FALLBACK_PATCHED = True
            return True
        orig_filter = cur

        def _src_series(df: pd.DataFrame):
            return df["source"].astype(str).str.lower().str.strip()

        def _patched_filter_push_like_rows(df: pd.DataFrame) -> pd.DataFrame:
            try:
                x = fl.normalize_df(df)
                if x.empty or "source" not in x.columns:
                    return x
                src = _src_series(x)
                real_push_mask = (
                    src.eq("push")
                    | src.str.contains("push_stream", na=False)
                    | src.str.contains("incremental", na=False)
                    | src.str.contains("summary_recovery_push", na=False)
                    | src.str.contains("resample", na=False)
                ) & ~src.str.contains("summary_recovery_yahoo", na=False)
                real = x.loc[real_push_mask].copy()
                if not real.empty:
                    logger.info(
                        "[summary.fallback_loader] real-push priority rows=%s -> %s source_dist=%s",
                        len(x),
                        len(real),
                        real["source"].astype(str).value_counts().head(10).to_dict(),
                    )
                    return real.reset_index(drop=True)

                if callable(orig_filter):
                    return orig_filter(x)
                return x.reset_index(drop=True)
            except Exception:
                logger.exception("[summary.fallback_loader] real-push priority filter failed")
                if callable(orig_filter):
                    try:
                        return orig_filter(df)
                    except Exception:
                        pass
                return df

        _patched_filter_push_like_rows._real_push_priority_v1 = True  # type: ignore[attr-defined]
        _patched_filter_push_like_rows._original = orig_filter  # type: ignore[attr-defined]
        fl.filter_push_like_rows = _patched_filter_push_like_rows
        _SUMMARY_FALLBACK_PATCHED = True
        logger.warning("[RANKING STUCK PENDING] patched summary.fallback_loader real push priority v1")
        return True
    except Exception:
        logger.exception("[RANKING STUCK PENDING] summary fallback loader patch failed")
        return False


def _patch_once() -> bool:
    try:
        _install_min_pending_patch()
        _force_ranking_runtime_caps()
        _patch_summary_fallback_loader()
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_stuck_pending_prune_v7", False):
            return True
        orig = getattr(cur, "_original", cur)

        def patched():
            global _RUN_STARTED_AT, _RUN_SEQ

            if _main_skip_ranking_entry():
                logger.warning(
                    "[RANKING STUCK PENDING] main.py skip ranking entry job mode=%s. "
                    "Set AUTOSTOCK_MAIN_OPERATION_MODE=full or AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY=0 to restore.",
                    _operation_mode(),
                )
                return 0

            caps = _force_ranking_runtime_caps()
            if not _RUN_LOCK.acquire(blocking=False):
                _clear_runtime_overlap_if_stale()
                return 0

            _RUN_SEQ += 1
            seq = _RUN_SEQ
            _RUN_STARTED_AT = time.time()
            join_sec = max(15.0, min(float(caps.get("runtime_budget") or 25.0), 25.0))
            state: dict[str, Any] = {"done": False, "ret": 0, "exc": None}

            def _worker() -> None:
                global _RUN_STARTED_AT
                try:
                    cnt = _pending_count()
                    if cnt > 0:
                        pruned = _mark_and_prune_stuck_ranking_pending()
                        if pruned:
                            left = _pending_count()
                            logger.warning(
                                "[RANKING STUCK PENDING] pre-build pruned=%s before=%s after=%s",
                                pruned,
                                cnt,
                                left,
                            )
                    state["ret"] = orig()
                except Exception as exc:
                    state["exc"] = exc
                    logger.exception("[RANKING STUCK PENDING] ranking_entry worker failed seq=%s", seq)
                finally:
                    elapsed = time.time() - float(_RUN_STARTED_AT or time.time())
                    state["done"] = True
                    if elapsed >= join_sec:
                        logger.warning(
                            "[RANKING STUCK PENDING] ranking_entry worker finished slow seq=%s elapsed=%.1fs join_sec=%.1fs",
                            seq,
                            elapsed,
                            join_sec,
                        )
                    _RUN_STARTED_AT = 0.0
                    try:
                        _RUN_LOCK.release()
                    except RuntimeError:
                        pass

            th = threading.Thread(target=_worker, name=f"ranking-entry-timebox-{seq}", daemon=True)
            th.start()
            th.join(join_sec)
            if th.is_alive():
                logger.warning(
                    "[RANKING STUCK PENDING] ranking_entry timeboxed seq=%s join_sec=%.1fs; return now, worker continues guarded by lock caps=%s",
                    seq,
                    join_sec,
                    caps,
                )
                return 0
            if state.get("exc") is not None:
                raise state["exc"]
            return state.get("ret", 0)

        patched._ranking_stuck_pending_prune_v1 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v2 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v3 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v4 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v5 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v6 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v7 = True  # type: ignore[attr-defined]
        patched._original = orig  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = patched
        logger.warning(
            "[RANKING STUCK PENDING] patched _run_ranking_entry_safe v7 timebox=25s overlap_guard=True caps=%s min_pending=%s main_skip=%s mode=%s",
            _force_ranking_runtime_caps(),
            _MIN_PENDING_PATCHED,
            _main_skip_ranking_entry(),
            _operation_mode(),
        )
        return True
    except Exception:
        logger.exception("[RANKING STUCK PENDING] patch failed")
        return False


def _watch() -> None:
    loops = max(1, min(_env_int("RANKING_STUCK_PENDING_ENFORCE_LOOPS", 30), 120))
    sleep_sec = max(1.0, min(_env_float("RANKING_STUCK_PENDING_ENFORCE_SLEEP_SEC", 3.0), 10.0))
    for i in range(loops):
        ok = _patch_once()
        if i in (0, loops - 1):
            logger.warning("[RANKING STUCK PENDING] enforce ok=%s v7 main_skip=%s mode=%s summary_fallback=%s min_pending=%s", ok, _main_skip_ranking_entry(), _operation_mode(), _SUMMARY_FALLBACK_PATCHED, _MIN_PENDING_PATCHED)
        time.sleep(sleep_sec)


def install() -> bool:
    global _DONE
    if _DONE:
        ok = _patch_once()
        return bool(ok)
    ok = _patch_once()
    threading.Thread(target=_watch, name="ranking-stuck-pending-prune-watch", daemon=True).start()
    _DONE = True
    logger.warning("[RANKING STUCK PENDING] installed v7 ok=%s watcher=True timebox=25s summary_fallback=%s min_pending=%s main_skip=%s mode=%s", ok, _SUMMARY_FALLBACK_PATCHED, _MIN_PENDING_PATCHED, _main_skip_ranking_entry(), _operation_mode())
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[RANKING STUCK PENDING] auto install failed")

__all__ = ["install"]
