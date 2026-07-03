# ============================================================
# File   : core/startup/tonosama_orphan_thread_failopen_patch.py
# Version: V1-ORPHAN-THREAD-AGE-FAILOPEN
# ------------------------------------------------------------
# Tonosama entry scheduler が timeout 後の orphan thread を見続けて
# previous_timeout_thread_still_alive で永続スキップする事故を防ぐ。
#
# 方針:
#   - ガードは削除しない。
#   - timeout直後の短時間は従来どおり二重実行を避ける。
#   - ただし orphan が一定秒数を超えて生存している場合は参照を解除し、
#     次回の Tonosama entry を許可する。
#   - 古い orphan thread は daemon thread なのでプロセス終了をブロックしない。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-ORPHAN-THREAD-AGE-FAILOPEN"
_PATCHED = False
_WATCHER_STARTED = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _now() -> dt.datetime:
    return dt.datetime.now()


def _clear_stale_orphan(tasks: Any, *, reason: str) -> bool:
    try:
        max_age = max(5.0, _env_float("TONOSAMA_ORPHAN_THREAD_MAX_AGE_SEC", 45.0))
        th = getattr(tasks, "_TONOSAMA_ENTRY_ORPHAN_THREAD", None)
        if th is None:
            return False
        if not getattr(th, "is_alive", lambda: False)():
            setattr(tasks, "_TONOSAMA_ENTRY_ORPHAN_THREAD", None)
            logger.warning("[TONOSAMA ORPHAN FAILOPEN] cleared dead orphan reason=%s version=%s", reason, VERSION)
            return True
        started_at = getattr(tasks, "_TONOSAMA_ENTRY_STARTED_AT", None)
        cooldown_until = getattr(tasks, "_TONOSAMA_ENTRY_COOLDOWN_UNTIL", None)
        age = None
        try:
            if isinstance(started_at, dt.datetime):
                age = max(0.0, (_now() - started_at).total_seconds())
        except Exception:
            age = None
        # If started_at is already cleared in finally, use cooldown expiry as a conservative proxy.
        if age is None and isinstance(cooldown_until, dt.datetime):
            base_cool = _env_float("TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC", 45.0)
            age = max(0.0, base_cool + (_now() - cooldown_until).total_seconds())
        if age is None:
            age = max_age + 1.0
        if age >= max_age:
            setattr(tasks, "_TONOSAMA_ENTRY_ORPHAN_THREAD", None)
            setattr(tasks, "_TONOSAMA_ENTRY_RUNNING", False)
            setattr(tasks, "_TONOSAMA_ENTRY_STARTED_AT", None)
            logger.warning(
                "[TONOSAMA ORPHAN FAILOPEN] cleared stale alive orphan reason=%s thread=%s age=%.1fs max_age=%.1fs version=%s",
                reason,
                getattr(th, "name", repr(th)),
                age,
                max_age,
                VERSION,
            )
            return True
    except Exception:
        logger.exception("[TONOSAMA ORPHAN FAILOPEN] clear failed reason=%s", reason)
    return False


def _patch_once(reason: str = "install") -> bool:
    try:
        if not _env_bool("TONOSAMA_ORPHAN_THREAD_FAILOPEN_ENABLED", True):
            logger.warning("[TONOSAMA ORPHAN FAILOPEN] disabled by env")
            return False
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, "_run_tonosama_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_tonosama_orphan_failopen_v1", False):
            _clear_stale_orphan(tasks, reason=f"already:{reason}")
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched(*args, **kwargs):
            try:
                _clear_stale_orphan(tasks, reason="before_run")
            except Exception:
                pass
            return orig(*args, **kwargs)

        patched._tonosama_orphan_failopen_v1 = True  # type: ignore[attr-defined]
        patched._original = orig  # type: ignore[attr-defined]
        tasks._run_tonosama_entry_safe = patched
        _clear_stale_orphan(tasks, reason=reason)
        logger.warning("[TONOSAMA ORPHAN FAILOPEN] installed reason=%s version=%s max_age=%s", reason, VERSION, os.getenv("TONOSAMA_ORPHAN_THREAD_MAX_AGE_SEC", "45"))
        return True
    except Exception:
        logger.exception("[TONOSAMA ORPHAN FAILOPEN] install failed reason=%s", reason)
        return False


def _watcher() -> None:
    interval = max(2.0, _env_float("TONOSAMA_ORPHAN_THREAD_WATCH_INTERVAL_SEC", 5.0))
    while _env_bool("TONOSAMA_ORPHAN_THREAD_FAILOPEN_ENABLED", True):
        _patch_once("watcher")
        time.sleep(interval)


def install() -> bool:
    global _PATCHED, _WATCHER_STARTED
    ok = _patch_once("install")
    _PATCHED = bool(ok)
    if ok and not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        try:
            threading.Thread(target=_watcher, name="tonosama-orphan-failopen-watch", daemon=True).start()
            logger.warning("[TONOSAMA ORPHAN FAILOPEN] watcher started version=%s", VERSION)
        except Exception:
            logger.debug("[TONOSAMA ORPHAN FAILOPEN] watcher start failed", exc_info=True)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[TONOSAMA ORPHAN FAILOPEN] auto install failed")


__all__ = ["install", "VERSION"]
