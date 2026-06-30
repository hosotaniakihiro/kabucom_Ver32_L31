# ============================================================
# File   : core/startup/entry_controller_stale_lock_recover_patch.py
# Version: v1-STALE-PIPELINE-LOCK-RECOVER
# ------------------------------------------------------------
# Purpose:
#   entry_controller._pipeline_lock が長時間保持されたままになり、
#   SUMMARY AI が entry_controller_lock_timeout で発注まで進まない問題を防ぐ。
#
# Behavior:
#   - 通常時は Lock と同じ acquire/release/locked を提供する。
#   - ロック保持時間が ENTRY_PIPELINE_LOCK_STALE_SEC を超えた場合、
#     次の acquire/locked で stale とみなし解除可能にする。
#   - 古い保持スレッドが後から release しても、現在の保持者と違えば無視する。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_SET:
            return True
        if s in _FALSE_SET:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


class _RecoverablePipelineLock:
    def __init__(self, original: Any | None = None) -> None:
        self._guard = threading.RLock()
        self._locked = False
        self._owner_ident: int | None = None
        self._owner_name: str | None = None
        self._acquired_at: float | None = None
        self._original = original
        self._stale_resets = 0

    def _stale_sec(self) -> float:
        return max(5.0, _env_float("ENTRY_PIPELINE_LOCK_STALE_SEC", 20.0))

    def _age(self) -> float | None:
        if self._acquired_at is None:
            return None
        return time.monotonic() - self._acquired_at

    def _is_stale_locked(self) -> bool:
        age = self._age()
        return bool(self._locked and age is not None and age >= self._stale_sec())

    def _force_reset_locked(self, reason: str) -> None:
        age = self._age()
        self._stale_resets += 1
        logger.warning(
            "[ENTRY PIPELINE LOCK RECOVER] stale lock reset reason=%s age=%s stale_sec=%.1f owner=%s owner_ident=%s resets=%s",
            reason,
            None if age is None else round(age, 3),
            self._stale_sec(),
            self._owner_name,
            self._owner_ident,
            self._stale_resets,
        )
        self._locked = False
        self._owner_ident = None
        self._owner_name = None
        self._acquired_at = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        cur = threading.current_thread()
        deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
        while True:
            with self._guard:
                if self._is_stale_locked():
                    self._force_reset_locked("acquire")
                if not self._locked:
                    self._locked = True
                    self._owner_ident = cur.ident
                    self._owner_name = cur.name
                    self._acquired_at = time.monotonic()
                    return True
            if not blocking:
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def release(self) -> None:
        cur = threading.current_thread()
        with self._guard:
            if not self._locked:
                logger.warning(
                    "[ENTRY PIPELINE LOCK RECOVER] release ignored because already unlocked caller=%s ident=%s",
                    cur.name,
                    cur.ident,
                )
                return
            if self._owner_ident is not None and self._owner_ident != cur.ident:
                logger.warning(
                    "[ENTRY PIPELINE LOCK RECOVER] stale owner release ignored caller=%s ident=%s current_owner=%s owner_ident=%s age=%s",
                    cur.name,
                    cur.ident,
                    self._owner_name,
                    self._owner_ident,
                    None if self._age() is None else round(self._age() or 0.0, 3),
                )
                return
            self._locked = False
            self._owner_ident = None
            self._owner_name = None
            self._acquired_at = None

    def locked(self) -> bool:
        with self._guard:
            if self._is_stale_locked():
                self._force_reset_locked("locked")
                return False
            return bool(self._locked)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_PIPELINE_LOCK_RECOVER_ENABLED", True):
        logger.warning("[ENTRY PIPELINE LOCK RECOVER] disabled by env")
        return False
    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY PIPELINE LOCK RECOVER] entry_controller import failed")
        return False

    try:
        cur = getattr(ec, "_pipeline_lock", None)
        if getattr(cur, "_entry_pipeline_lock_recover_v1", False):
            _INSTALLED = True
            return True
        replacement = _RecoverablePipelineLock(cur)
        replacement._entry_pipeline_lock_recover_v1 = True  # type: ignore[attr-defined]
        ec._pipeline_lock = replacement
        os.environ.setdefault("ENTRY_PIPELINE_LOCK_STALE_SEC", "20")
        _INSTALLED = True
        logger.warning(
            "[ENTRY PIPELINE LOCK RECOVER] installed stale_sec=%s original=%s",
            os.environ.get("ENTRY_PIPELINE_LOCK_STALE_SEC"),
            type(cur).__name__,
        )
        return True
    except Exception:
        logger.exception("[ENTRY PIPELINE LOCK RECOVER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY PIPELINE LOCK RECOVER] auto install failed")


__all__ = ["install"]
