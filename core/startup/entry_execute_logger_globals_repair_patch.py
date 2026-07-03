# ============================================================
# File   : core/startup/entry_execute_logger_globals_repair_patch.py
# Version: V1-LOGGER-GLOBALS-REPAIR
# ------------------------------------------------------------
# Runtime repair for entry_controller._execute_best_candidate globals.
#
# Problem:
#   Some wrapper chains call a pinned/cloned true-original function whose
#   __globals__ can miss names such as logger.  Then order execution reaches
#   quantity calculation and fails with:
#     NameError("name 'logger' is not defined")
#
# Fix:
#   Repair globals on entry_controller._execute_best_candidate, the pinned
#   _BASE_EXECUTE_BEST_CANDIDATE, and known original pointers.  Keep this small
#   and fail-open; it does not relax any trading guard.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
VERSION = "V1-LOGGER-GLOBALS-REPAIR"
_PATCHED = False
_WATCHER_STARTED = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}

_ORIGINAL_ATTRS = (
    "_BASE_EXECUTE_BEST_CANDIDATE",
    "_entry_execute_timeout_guard_original",
    "_original_execute_best_candidate",
    "_final_entry_safety_guard_original",
    "_summary_ai_entry_bridge_original",
    "_summary_ai_entry_controller_bridge_original",
    "_original",
    "__wrapped__",
)

_REQUIRED_GLOBALS = (
    "logger",
    "_resolve_price",
    "calculate_entry_quantity",
    "BOOST_SIZE_MULTIPLIER",
    "MIN_ENTRY_QTY",
    "build_entry_order",
    "_safe_int",
    "_safe_str",
    "place_entry_buy",
    "place_entry_sell",
    "global_data",
)


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


def _iter_function_chain(root: Any) -> list[Callable[..., Any]]:
    out: list[Callable[..., Any]] = []
    seen: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        fn = stack.pop(0)
        if not callable(fn) or id(fn) in seen:
            continue
        seen.add(id(fn))
        out.append(fn)
        for attr in _ORIGINAL_ATTRS:
            try:
                nxt = getattr(fn, attr, None)
            except Exception:
                nxt = None
            if callable(nxt) and id(nxt) not in seen:
                stack.append(nxt)
    return out


def _repair_function_globals(fn: Callable[..., Any], ec: Any, *, reason: str) -> list[str]:
    try:
        g = getattr(fn, "__globals__", None)
        if not isinstance(g, dict):
            return []
        repaired: list[str] = []
        for name in _REQUIRED_GLOBALS:
            if name not in g or g.get(name) is None:
                if hasattr(ec, name):
                    g[name] = getattr(ec, name)
                    repaired.append(name)
        if "logger" not in g or g.get("logger") is None:
            g["logger"] = logging.getLogger("entry_controller")
            repaired.append("logger:fallback")
        if repaired:
            logger.warning(
                "[ENTRY EXEC LOGGER REPAIR] repaired reason=%s target=%s names=%s version=%s",
                reason,
                getattr(fn, "__name__", repr(fn)),
                repaired,
                VERSION,
            )
        return repaired
    except Exception:
        logger.debug("[ENTRY EXEC LOGGER REPAIR] function repair failed reason=%s", reason, exc_info=True)
        return []


def repair(reason: str = "manual") -> bool:
    try:
        import trading.handlers.entry_controller as ec
        roots: list[Any] = [getattr(ec, "_execute_best_candidate", None), getattr(ec, "_BASE_EXECUTE_BEST_CANDIDATE", None)]
        repaired_total: list[str] = []
        for root in roots:
            for fn in _iter_function_chain(root):
                if getattr(fn, "__name__", "") == "_execute_best_candidate" or fn in roots:
                    repaired_total.extend(_repair_function_globals(fn, ec, reason=reason))
        if repaired_total:
            logger.warning("[ENTRY EXEC LOGGER REPAIR] done reason=%s repaired_count=%s version=%s", reason, len(repaired_total), VERSION)
        return True
    except Exception:
        logger.exception("[ENTRY EXEC LOGGER REPAIR] repair failed reason=%s", reason)
        return False


def _watcher() -> None:
    interval = 1.0
    try:
        interval = max(1.0, float(os.getenv("ENTRY_EXEC_LOGGER_REPAIR_WATCHER_INTERVAL_SEC", "2")))
    except Exception:
        pass
    while _env_bool("ENTRY_EXEC_LOGGER_REPAIR_WATCHER_ENABLED", True):
        repair("watcher")
        time.sleep(interval)


def install() -> bool:
    global _PATCHED, _WATCHER_STARTED
    if not _env_bool("ENTRY_EXEC_LOGGER_REPAIR_ENABLED", True):
        logger.warning("[ENTRY EXEC LOGGER REPAIR] disabled by env")
        return False
    ok = repair("install")
    _PATCHED = bool(ok)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        try:
            threading.Thread(target=_watcher, name="entry-exec-logger-repair-watch", daemon=True).start()
            logger.warning("[ENTRY EXEC LOGGER REPAIR] watcher started version=%s", VERSION)
        except Exception:
            logger.debug("[ENTRY EXEC LOGGER REPAIR] watcher start failed", exc_info=True)
    logger.warning("[ENTRY EXEC LOGGER REPAIR] installed ok=%s version=%s", ok, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[ENTRY EXEC LOGGER REPAIR] auto install failed")


__all__ = ["install", "repair", "VERSION"]
