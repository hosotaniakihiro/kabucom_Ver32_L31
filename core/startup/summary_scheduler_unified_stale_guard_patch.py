# ============================================================
# File   : core/startup/summary_scheduler_unified_stale_guard_patch.py
# Version: V1-PARENT-TICK-STALE-RESET
# ============================================================
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)
_PATCHED = False


def _b(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, '')).strip().lower()
        if raw in ('1', 'true', 'yes', 'on', 'enable', 'enabled'):
            return True
        if raw in ('0', 'false', 'no', 'off', 'disable', 'disabled'):
            return False
    except Exception:
        pass
    return bool(default)


def _f(name: str, default: float) -> float:
    try:
        raw = str(os.getenv(name, '')).strip()
        if raw:
            return max(1.0, float(raw))
    except Exception:
        pass
    return float(default)


def _main_entry_only() -> bool:
    try:
        if _b('AUTOSTOCK_MAIN_DATABASE_PROCESS', False) or _b('AUTOSTOCK_DATA_COLLECTORS_PROCESS', False):
            return False
        role = str(os.getenv('SUMMARY_DB_WRITER_ROLE') or '').strip().lower()
        return _b('SUMMARY_MAIN_ENTRY_ONLY', False) or role in ('entry_only', 'main_entry_only', 'read_only', 'no_save')
    except Exception:
        return False


def _push_bg_all() -> bool:
    return _b('SUMMARY_PUSH_BG_ALL_INTERVALS', _main_entry_only())


def _suppress_fallback() -> bool:
    return _main_entry_only() and _push_bg_all()


def _reset_unified_if_stale(scheduler, context: str) -> None:
    try:
        if not bool(getattr(scheduler, '_unified_bg_running', False)):
            return
        started = getattr(scheduler, '_unified_bg_started_at', None)
        elapsed = 999999.0 if started is None else max(0.0, time.time() - float(started))
        stale_sec = _f('SUMMARY_UNIFIED_BG_STALE_SEC', 60.0)
        if elapsed < stale_sec:
            return
        lock = getattr(scheduler, '_unified_bg_lock', None)
        if lock is not None:
            with lock:
                scheduler._unified_bg_running = False
                scheduler._unified_bg_started_at = None
        else:
            scheduler._unified_bg_running = False
            scheduler._unified_bg_started_at = None
        logger.warning('[SUMMARY SCHEDULER STALE GUARD] unified reset context=%s elapsed=%.3fs stale=%.3fs', context, elapsed, stale_sec)
    except Exception:
        logger.exception('[SUMMARY SCHEDULER STALE GUARD] reset failed context=%s', context)


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import scheduler_jobs.summary.scheduler as scheduler
    except Exception:
        logger.exception('[SUMMARY SCHEDULER STALE GUARD] import failed')
        return False

    try:
        if _suppress_fallback() and not str(os.getenv('ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED') or '').strip():
            os.environ['ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED'] = '0'
            logger.warning('[SUMMARY SCHEDULER STALE GUARD] default fallback disabled for push_bg_all')

        _reset_unified_if_stale(scheduler, 'install')

        old_enabled = getattr(scheduler, '_push_fallback_when_blocked_enabled', None)
        if callable(old_enabled) and not getattr(old_enabled, '_stale_guard_patch', False):
            def enabled_patched():
                if _suppress_fallback():
                    logger.info('[SUMMARY SCHEDULER STALE GUARD] legacy PUSH fallback suppressed')
                    return False
                try:
                    return bool(old_enabled())
                except Exception:
                    return False
            enabled_patched._stale_guard_patch = True
            enabled_patched._original = old_enabled
            scheduler._push_fallback_when_blocked_enabled = enabled_patched

        old_tick = getattr(scheduler, '_run_summary_tick', None)
        if callable(old_tick) and not getattr(old_tick, '_stale_guard_patch', False):
            def tick_patched(now=None):
                _reset_unified_if_stale(scheduler, 'before_parent_tick')
                return old_tick(now=now)
            tick_patched._stale_guard_patch = True
            tick_patched._original = old_tick
            scheduler._run_summary_tick = tick_patched

        _PATCHED = True
        logger.warning('[SUMMARY SCHEDULER STALE GUARD] installed suppress_fallback=%s main_entry_only=%s push_bg_all=%s stale_sec=%.1f', _suppress_fallback(), _main_entry_only(), _push_bg_all(), _f('SUMMARY_UNIFIED_BG_STALE_SEC', 60.0))
        return True
    except Exception:
        logger.exception('[SUMMARY SCHEDULER STALE GUARD] install failed')
        return False

try:
    install()
except Exception:
    logger.exception('[SUMMARY SCHEDULER STALE GUARD] auto install failed')

__all__ = ['install']
