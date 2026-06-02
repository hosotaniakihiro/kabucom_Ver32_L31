# ============================================================
# File   : core/startup/tonosama_controller_timeout_patch.py
# Version: V1-TONOSAMA-CONTROLLER-TIMEOUT-35S
# ------------------------------------------------------------
# Purpose:
#   Tonosama pending build can now finish quickly, but the subsequent
#   entry_controller path may spend time in lock wait / board fetch /
#   final safety guard. 12s is too short and causes:
#
#     [TONOSAMA ENTRY SCHEDULE CONTROLLER] timeout -> return to scheduler
#     [TONOSAMA ENTRY SCHEDULE] controller timeout pipeline_source=TONOSAMA
#
#   The controller thread remains alive, so scheduler state becomes noisy and
#   pending can stay behind.  Force Tonosama controller timeout to at least 35s.
#
# Notes:
#   - Ranking timeout is not changed.
#   - If the module is not imported yet, retry briefly in a daemon thread.
#   - Also wraps _dispatch_entry_controller as a guard in case the constant was
#     captured before this patch runs.
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_INSTALLING = False
_ORIG_DISPATCH = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _min_timeout() -> float:
    return max(35.0, _env_float("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", 35.0))


def _apply() -> bool:
    global _INSTALLED, _ORIG_DISPATCH
    if _INSTALLED:
        return True
    try:
        import trading.entry_exit.tasks as tasks
    except Exception:
        logger.debug("[TONOSAMA CONTROLLER TIMEOUT PATCH] tasks not ready", exc_info=True)
        return False

    try:
        min_sec = _min_timeout()
        old = float(getattr(tasks, "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", 0.0) or 0.0)
        if old < min_sec:
            setattr(tasks, "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", min_sec)

        cur = getattr(tasks, "_dispatch_entry_controller", None)
        if not callable(cur):
            logger.warning("[TONOSAMA CONTROLLER TIMEOUT PATCH] dispatch target missing")
            return False
        if getattr(cur, "_tonosama_controller_timeout_patch_v1", False):
            _INSTALLED = True
            return True

        _ORIG_DISPATCH = cur

        def _dispatch_entry_controller_patched(*args: Any, **kwargs: Any) -> bool:
            try:
                pipeline_source = str(kwargs.get("pipeline_source") or "").upper()
                if not pipeline_source and args:
                    # normally keyword-only, defensive only
                    pipeline_source = str(args[0] or "").upper()
                if pipeline_source == "TONOSAMA":
                    requested = float(kwargs.get("timeout_sec") or 0.0)
                    forced = max(_min_timeout(), requested)
                    if forced != requested:
                        logger.warning(
                            "[TONOSAMA CONTROLLER TIMEOUT PATCH] force controller timeout %.3f -> %.3f",
                            requested,
                            forced,
                        )
                    kwargs["timeout_sec"] = forced
                    try:
                        setattr(tasks, "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", forced)
                    except Exception:
                        pass
            except Exception:
                logger.debug("[TONOSAMA CONTROLLER TIMEOUT PATCH] timeout force failed", exc_info=True)
            return _ORIG_DISPATCH(*args, **kwargs)

        _dispatch_entry_controller_patched._tonosama_controller_timeout_patch_v1 = True  # type: ignore[attr-defined]
        _dispatch_entry_controller_patched._original = cur  # type: ignore[attr-defined]
        setattr(tasks, "_dispatch_entry_controller", _dispatch_entry_controller_patched)
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA CONTROLLER TIMEOUT PATCH] installed v1 old=%.3f new=%.3f env=%s",
            old,
            float(getattr(tasks, "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC", min_sec) or min_sec),
            os.getenv("TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA CONTROLLER TIMEOUT PATCH] apply failed")
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLING
    if _apply():
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _retry_loop() -> None:
            global _INSTALLING
            try:
                for _ in range(80):
                    if _apply():
                        return
                    time.sleep(0.2)
                logger.warning("[TONOSAMA CONTROLLER TIMEOUT PATCH] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_retry_loop, name="tonosama-controller-timeout-patch", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA CONTROLLER TIMEOUT PATCH] auto install failed")


__all__ = ["install"]
