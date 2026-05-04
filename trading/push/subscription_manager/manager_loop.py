# ============================================================
# File   : trading/push/subscription_manager/manager_loop.py
# Version: V1.0-PUSH-SUBSCRIPTION-MANAGER-LOOP
# ------------------------------------------------------------
# Purpose:
#   - background subscription refresh loop
# ============================================================

from __future__ import annotations

import logging
import threading

from . import state

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_SEC = 60.0


def manager_loop(interval_sec: float = DEFAULT_REFRESH_INTERVAL_SEC) -> None:
    """
    循環importを避けるため、loop内で core.refresh_subscriptions をimportする。
    """
    logger.info("[SUB MANAGER LOOP] started interval=%.1fs", interval_sec)

    while not state.manager_stop_event.is_set():
        try:
            from .core import refresh_subscriptions

            refresh_subscriptions(reason="background_loop")
        except Exception:
            logger.exception("[SUB MANAGER LOOP] background refresh failed")

        state.manager_stop_event.wait(interval_sec)

    logger.info("[SUB MANAGER LOOP] stopped")


def start_symbol_subscription_manager(
    interval_sec: float = DEFAULT_REFRESH_INTERVAL_SEC,
) -> bool:
    with state.manager_lock:
        if state.manager_thread is not None and state.manager_thread.is_alive():
            logger.info("[SUB MANAGER LOOP] already started")
            return True

        state.manager_stop_event.clear()
        state.manager_thread = threading.Thread(
            target=manager_loop,
            kwargs={"interval_sec": interval_sec},
            daemon=True,
            name="symbol_subscription_manager",
        )
        state.manager_thread.start()
        state.started = True

    logger.info("[SUB MANAGER LOOP] start requested interval=%.1fs", interval_sec)
    return True


def stop_symbol_subscription_manager(timeout: float = 5.0) -> bool:
    with state.manager_lock:
        t = state.manager_thread
        state.manager_stop_event.set()

    if t is not None and t.is_alive():
        t.join(timeout=timeout)

    with state.manager_lock:
        state.manager_thread = None
        state.started = False

    logger.info("[SUB MANAGER LOOP] stopped")
    return True


__all__ = [
    "DEFAULT_REFRESH_INTERVAL_SEC",
    "manager_loop",
    "start_symbol_subscription_manager",
    "stop_symbol_subscription_manager",
]
