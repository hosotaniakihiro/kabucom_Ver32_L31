# ============================================================
# File   : core/startup/tonosama_orphan_timeout_prune_patch.py
# Version: V1-PRUNE-STALE-TIMEOUT-ORPHAN
# ------------------------------------------------------------
# Tonosama pending build が timeout した後、古い daemon thread が
# is_alive=True のまま残り続けて次回スケジュールを永続skipする症状を防ぐ。
# 一定時間を超えた timeout orphan は切り離し、次サイクルを許可する。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
VERSION = "V1-PRUNE-STALE-TIMEOUT-ORPHAN"
_INSTALLED = False
_WATCHER_STARTED = False
_ORPHAN_FIRST_SEEN: dict[int, float] = {}
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _tasks():
    import trading.entry_exit.tasks as tasks
    return tasks


def _thread_key(th: threading.Thread) -> int:
    try:
        return int(th.ident or id(th))
    except Exception:
        return id(th)


def _prune_if_stale(tasks: Any, *, reason: str) -> bool:
    try:
        th = getattr(tasks, "_TONOSAMA_ENTRY_ORPHAN_THREAD", None)
        if th is None:
            return False
        if not getattr(th, "is_alive", lambda: False)():
            setattr(tasks, "_TONOSAMA_ENTRY_ORPHAN_THREAD", None)
            logger.warning("[TONOSAMA ORPHAN PRUNE] cleared dead orphan reason=%s thread=%s version=%s", reason, getattr(th, "name", None), VERSION)
            return True
        key = _thread_key(th)
        now = time.monotonic()
        first = _ORPHAN_FIRST_SEEN.setdefault(key, now)
        age = now - first
        max_age = max(5.0, _env_float("TONOSAMA_ORPHAN_THREAD_MAX_ALIVE_SEC", 65.0))
        if age < max_age:
            logger.warning("[TONOSAMA ORPHAN PRUNE] keep live orphan reason=%s thread=%s orphan_age=%.1fs max_age=%.1fs", reason, getattr(th, "name", None), age, max_age)
            return False
        # Python thread cannot be killed safely.  Treat it as detached work so scheduler can evaluate fresh data.
        setattr(tasks, "_TONOSAMA_ENTRY_ORPHAN_THREAD", None)
        try:
            setattr(tasks, "_TONOSAMA_ENTRY_COOLDOWN_UNTIL", None)
        except Exception:
            pass
        try:
            setattr(tasks, "_TONOSAMA_ENTRY_TIMEOUT_STREAK", 0)
        except Exception:
            pass
        logger.warning("[TONOSAMA ORPHAN PRUNE] detached stale live orphan reason=%s thread=%s orphan_age=%.1fs max_age=%.1fs version=%s", reason, getattr(th, "name", None), age, max_age, VERSION)
        return True
    except Exception:
        logger.exception("[TONOSAMA ORPHAN PRUNE] prune failed reason=%s", reason)
        return False


def _wrap_run(fn: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(fn, "_tonosama_orphan_prune_v1", False):
        return fn

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        tasks = _tasks()
        _prune_if_stale(tasks, reason="before_run")
        return fn(*args, **kwargs)

    wrapped._tonosama_orphan_prune_v1 = True  # type: ignore[attr-defined]
    wrapped._original = fn  # type: ignore[attr-defined]
    return wrapped


def _apply_once(reason: str) -> bool:
    try:
        tasks = _tasks()
        cur = getattr(tasks, "_run_tonosama_entry_safe", None)
        if not callable(cur):
            logger.warning("[TONOSAMA ORPHAN PRUNE] target missing reason=%s", reason)
            return False
        if getattr(cur, "_tonosama_orphan_prune_v1", False):
            _prune_if_stale(tasks, reason=f"already:{reason}")
            return True
        tasks._run_tonosama_entry_safe = _wrap_run(cur)
        _prune_if_stale(tasks, reason=reason)
        logger.warning("[TONOSAMA ORPHAN PRUNE] applied reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[TONOSAMA ORPHAN PRUNE] apply failed reason=%s", reason)
        return False


def _watcher() -> None:
    loops = int(max(1.0, _env_float("TONOSAMA_ORPHAN_PRUNE_WATCHER_LOOPS", 240.0)))
    sec = max(1.0, _env_float("TONOSAMA_ORPHAN_PRUNE_WATCHER_SEC", 2.0))
    for i in range(loops):
        try:
            _apply_once(f"watcher:{i}")
        except Exception:
            logger.exception("[TONOSAMA ORPHAN PRUNE] watcher failed")
        time.sleep(sec)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("TONOSAMA_ORPHAN_TIMEOUT_PRUNE_ENABLED", True):
        logger.warning("[TONOSAMA ORPHAN PRUNE] disabled by env")
        return False
    ok = _apply_once("install")
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED and _env_bool("TONOSAMA_ORPHAN_PRUNE_WATCHER_ENABLED", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="tonosama-orphan-prune-watcher", daemon=True).start()
    logger.warning("[TONOSAMA ORPHAN PRUNE] installed ok=%s version=%s", ok, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[TONOSAMA ORPHAN PRUNE] auto install failed")


__all__ = ["install", "VERSION"]
