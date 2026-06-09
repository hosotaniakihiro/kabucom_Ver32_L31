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


def _mark_and_prune_stuck_ranking_pending() -> int:
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

        removed = int(prune(pred, reason="RANKING_STUCK_PENDING_RETRY_OR_AGE"))
        if removed:
            logger.warning(
                "[RANKING STUCK PENDING] pruned removed=%s max_retry=%s min_age=%.1fs max_age=%.1fs",
                removed,
                max_retry,
                min_age_sec,
                max_age_sec,
            )
        return removed
    except Exception:
        logger.exception("[RANKING STUCK PENDING] prune failed")
        return 0


def _patch_once() -> bool:
    try:
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_stuck_pending_prune_v4", False):
            return True
        orig = getattr(cur, "_original", cur)

        def patched():
            if _main_skip_ranking_entry():
                logger.warning(
                    "[RANKING STUCK PENDING] main.py skip ranking entry job mode=%s. "
                    "Set AUTOSTOCK_MAIN_OPERATION_MODE=full or AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY=0 to restore.",
                    _operation_mode(),
                )
                return 0

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
            return orig()

        patched._ranking_stuck_pending_prune_v1 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v2 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v3 = True  # type: ignore[attr-defined]
        patched._ranking_stuck_pending_prune_v4 = True  # type: ignore[attr-defined]
        patched._original = orig  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = patched
        logger.warning("[RANKING STUCK PENDING] patched _run_ranking_entry_safe v4 min_age_guard=True main_skip=%s mode=%s", _main_skip_ranking_entry(), _operation_mode())
        return True
    except Exception:
        logger.exception("[RANKING STUCK PENDING] patch failed")
        return False


def _watch() -> None:
    loops = max(1, min(_env_int("RANKING_STUCK_PENDING_ENFORCE_LOOPS", 120), 300))
    sleep_sec = max(0.5, min(_env_float("RANKING_STUCK_PENDING_ENFORCE_SLEEP_SEC", 2.0), 10.0))
    for i in range(loops):
        ok = _patch_once()
        if i in (0, 1, 5, 30, loops - 1):
            logger.warning("[RANKING STUCK PENDING] enforce ok=%s v4 main_skip=%s mode=%s", ok, _main_skip_ranking_entry(), _operation_mode())
        time.sleep(sleep_sec)


def install() -> bool:
    global _DONE
    if _DONE:
        ok = _patch_once()
        logger.warning("[RANKING STUCK PENDING] enforce ok=%s v4 main_skip=%s mode=%s", ok, _main_skip_ranking_entry(), _operation_mode())
        return bool(ok)
    ok = _patch_once()
    threading.Thread(target=_watch, name="ranking-stuck-pending-prune-watch", daemon=True).start()
    _DONE = True
    logger.warning("[RANKING STUCK PENDING] installed v4 ok=%s watcher=True min_age_guard=True main_skip=%s mode=%s", ok, _main_skip_ranking_entry(), _operation_mode())
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[RANKING STUCK PENDING] auto install failed")

__all__ = ["install"]
