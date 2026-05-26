# ============================================================
# File   : core/startup/summary_scheduler_unified_stale_guard_patch.py
# Version: V2-HARD-SUPPRESS-LEGACY-PUSH-FALLBACK
# ------------------------------------------------------------
# 【目的】
#   main.py entry_only で SUMMARY_PARALLEL 側は no-wait 化していても、
#   scheduler_jobs.summary.scheduler 側の legacy PUSH fallback が
#   previous_unified_bg_still_running / unified timeout 時に起動し、
#   PUSH-1m を90秒待って詰まる問題を防止する。
#
# 【今回の症状】
#   [summary.scheduler] PUSH fallback worker start reason=previous_unified_bg_still_running
#   [summary.scheduler] TIMEOUT label=PUSH-1m timeout=90.0s
#
# 【方針】
#   - main.py entry_only + SUMMARY_PUSH_BG_ALL_INTERVALS=1 相当では
#     _run_push_fallback_when_unified_blocked を直接no-op化
#   - _push_fallback_when_blocked_enabled もFalseへ固定
#   - 既存のstale resetは維持
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
    if _b('FORCE_ENABLE_LEGACY_PUSH_SUMMARY_FALLBACK', False):
        return False
    return _main_entry_only() and _push_bg_all()


def _reset_push_fallback_state(scheduler, context: str) -> None:
    try:
        if hasattr(scheduler, '_mark_push_fallback_done') and callable(getattr(scheduler, '_mark_push_fallback_done')):
            scheduler._mark_push_fallback_done()
            logger.warning('[SUMMARY SCHEDULER STALE GUARD] push fallback state reset context=%s via helper', context)
            return
        if hasattr(scheduler, '_push_fallback_running'):
            scheduler._push_fallback_running = False
        if hasattr(scheduler, '_push_fallback_started_at'):
            scheduler._push_fallback_started_at = None
        if hasattr(scheduler, '_push_fallback_reason'):
            scheduler._push_fallback_reason = None
        if hasattr(scheduler, '_push_fallback_thread_name'):
            scheduler._push_fallback_thread_name = None
        logger.warning('[SUMMARY SCHEDULER STALE GUARD] push fallback state reset context=%s manual', context)
    except Exception:
        logger.exception('[SUMMARY SCHEDULER STALE GUARD] push fallback state reset failed context=%s', context)


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
        if _suppress_fallback():
            os.environ['ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED'] = '0'
            os.environ.setdefault('SUMMARY_PUSH_BG_ALL_INTERVALS', '1')
            logger.warning('[SUMMARY SCHEDULER STALE GUARD] legacy PUSH fallback hard-disabled for main entry process')
            _reset_push_fallback_state(scheduler, 'install_suppress')

        _reset_unified_if_stale(scheduler, 'install')

        old_enabled = getattr(scheduler, '_push_fallback_when_blocked_enabled', None)
        if callable(old_enabled) and not getattr(old_enabled, '_stale_guard_patch_v2', False):
            def enabled_patched():
                if _suppress_fallback():
                    logger.info('[SUMMARY SCHEDULER STALE GUARD] legacy PUSH fallback enabled? -> False')
                    return False
                try:
                    return bool(old_enabled())
                except Exception:
                    return False
            enabled_patched._stale_guard_patch_v2 = True
            enabled_patched._original = old_enabled
            scheduler._push_fallback_when_blocked_enabled = enabled_patched

        old_fallback = getattr(scheduler, '_run_push_fallback_when_unified_blocked', None)
        if callable(old_fallback) and not getattr(old_fallback, '_stale_guard_patch_v2', False):
            def fallback_patched(now, *, reason: str):
                if _suppress_fallback():
                    _reset_push_fallback_state(scheduler, f'suppressed:{reason}')
                    try:
                        hhmm = f'{now.hour:02d}:{now.minute:02d}'
                    except Exception:
                        hhmm = str(now)
                    logger.warning(
                        '[SUMMARY SCHEDULER STALE GUARD] legacy PUSH fallback suppressed reason=%s hhmm=%s main_entry_only=%s push_bg_all=%s',
                        reason, hhmm, _main_entry_only(), _push_bg_all(),
                    )
                    return None
                return old_fallback(now, reason=reason)
            fallback_patched._stale_guard_patch_v2 = True
            fallback_patched._original = old_fallback
            scheduler._run_push_fallback_when_unified_blocked = fallback_patched

        old_tick = getattr(scheduler, '_run_summary_tick', None)
        if callable(old_tick) and not getattr(old_tick, '_stale_guard_patch_v2', False):
            def tick_patched(now=None):
                _reset_unified_if_stale(scheduler, 'before_parent_tick')
                if _suppress_fallback():
                    _reset_push_fallback_state(scheduler, 'before_parent_tick_suppress')
                return old_tick(now=now)
            tick_patched._stale_guard_patch_v2 = True
            tick_patched._original = old_tick
            scheduler._run_summary_tick = tick_patched

        _PATCHED = True
        logger.warning(
            '[SUMMARY SCHEDULER STALE GUARD] installed v2 suppress_fallback=%s main_entry_only=%s push_bg_all=%s stale_sec=%.1f',
            _suppress_fallback(), _main_entry_only(), _push_bg_all(), _f('SUMMARY_UNIFIED_BG_STALE_SEC', 60.0),
        )
        return True
    except Exception:
        logger.exception('[SUMMARY SCHEDULER STALE GUARD] install failed')
        return False

try:
    install()
except Exception:
    logger.exception('[SUMMARY SCHEDULER STALE GUARD] auto install failed')

__all__ = ['install']