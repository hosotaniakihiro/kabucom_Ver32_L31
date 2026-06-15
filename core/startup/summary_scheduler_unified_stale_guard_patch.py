# ============================================================
# File   : core/startup/summary_scheduler_unified_stale_guard_patch.py
# Version: V5-MAIN-UNIFIED-STALE-FAST-RESET-NO-PREVIOUS-FALLBACK
# ------------------------------------------------------------
# 【目的】
#   main.py entry_only では scheduler_jobs.summary.scheduler の親tickが
#   NAS上の summary/push/cache を読みに行き 0xC0000006 で落ちる問題を防止する。
#
# V5:
#   - main.py では unified runner が残った状態を短時間で解除する。
#   - previous_unified_bg_still_running では PUSH fallback を追加起動しない。
#   - timeout済みexecutorスレッドが裏で残っても、親tickをfallback連鎖で詰まらせない。
#
# V4:
#   - main.py の既定運用を full とし、summary_parent_tick を復帰。
#   - AUTOSTOCK_MAIN_OPERATION_MODE=entry_only / safe のときだけ親tickをskip。
#   - usercustomize.py より先にこのpatchが入っても、勝手に
#     AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK=1 をsetdefaultしない。
# ============================================================
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_PATCHED = False

_TRUE = {'1', 'true', 'yes', 'on', 'enable', 'enabled'}
_FALSE = {'0', 'false', 'no', 'off', 'disable', 'disabled'}


def _b(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, '')).strip().lower()
        if raw in _TRUE:
            return True
        if raw in _FALSE:
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


def _argv_main_py() -> bool:
    try:
        return Path(sys.argv[0]).name.lower() == 'main.py'
    except Exception:
        return False


def _operation_mode() -> str:
    try:
        return str(os.getenv('AUTOSTOCK_MAIN_OPERATION_MODE') or 'full').strip().lower()
    except Exception:
        return 'full'


def _main_entry_only() -> bool:
    try:
        if _b('AUTOSTOCK_MAIN_DATABASE_PROCESS', False) or _b('AUTOSTOCK_DATA_COLLECTORS_PROCESS', False):
            return False
        role = str(os.getenv('SUMMARY_DB_WRITER_ROLE') or '').strip().lower()
        return _argv_main_py() or _b('SUMMARY_MAIN_ENTRY_ONLY', False) or role in ('entry_only', 'main_entry_only', 'read_only', 'no_save')
    except Exception:
        return False


def _main_skip_parent_tick() -> bool:
    if not _main_entry_only():
        return False
    if _b('FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK', False):
        return False
    mode = _operation_mode()
    if mode in ('full', 'restore', 'live', 'normal'):
        return _b('AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK', False)
    return _b('AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK', True)


def _push_bg_all() -> bool:
    return _b('SUMMARY_PUSH_BG_ALL_INTERVALS', _main_entry_only())


def _suppress_fallback() -> bool:
    if _b('FORCE_ENABLE_LEGACY_PUSH_SUMMARY_FALLBACK', False):
        return False
    return _main_entry_only() and _push_bg_all() and _main_skip_parent_tick()


def _main_unified_stale_sec() -> float:
    # main.py の親tickは前回の timeout cap が25秒なので、それより短めに解除する。
    # executor内の古い処理は強制停止できないが、schedulerの running フラグは解除して
    # previous_unified_bg_still_running -> fallback連鎖を止める。
    default = 20.0 if _main_entry_only() else 60.0
    return _f('SUMMARY_MAIN_UNIFIED_BG_STALE_SEC' if _main_entry_only() else 'SUMMARY_UNIFIED_BG_STALE_SEC', default)


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


def _reset_unified_if_stale(scheduler, context: str, *, force: bool = False) -> bool:
    try:
        if not bool(getattr(scheduler, '_unified_bg_running', False)):
            return False
        started = getattr(scheduler, '_unified_bg_started_at', None)
        elapsed = 999999.0 if started is None else max(0.0, time.time() - float(started))
        stale_sec = _main_unified_stale_sec()
        if not force and elapsed < stale_sec:
            return False
        lock = getattr(scheduler, '_unified_bg_lock', None)
        if lock is not None:
            with lock:
                scheduler._unified_bg_running = False
                scheduler._unified_bg_started_at = None
        else:
            scheduler._unified_bg_running = False
            scheduler._unified_bg_started_at = None
        logger.warning('[SUMMARY SCHEDULER STALE GUARD] unified reset context=%s elapsed=%.3fs stale=%.3fs force=%s', context, elapsed, stale_sec, force)
        return True
    except Exception:
        logger.exception('[SUMMARY SCHEDULER STALE GUARD] reset failed context=%s', context)
        return False


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
        if _main_entry_only():
            mode = _operation_mode()
            # main.py では前回のunified/fallback残りを早く解放する。
            os.environ.setdefault('SUMMARY_MAIN_UNIFIED_BG_STALE_SEC', '20')
            os.environ.setdefault('SUMMARY_PUSH_FALLBACK_STALE_SEC', '25')
            if mode not in ('full', 'restore', 'live', 'normal'):
                os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK', '1')
                os.environ['ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED'] = '0'
                os.environ.setdefault('SUMMARY_PUSH_BG_ALL_INTERVALS', '0')
                os.environ.setdefault('SUMMARY_PUSH_BG_LONG_INTERVALS', '0')
                logger.warning('[SUMMARY SCHEDULER STALE GUARD] main entry process: parent tick/fallback guarded mode=%s', mode)
            else:
                os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK', '0')
                os.environ.setdefault('FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK', '1')
                # full運用でも previous_unified_bg_still_running からのPUSH fallback連鎖は止める。
                os.environ.setdefault('SUMMARY_SUPPRESS_PREVIOUS_UNIFIED_FALLBACK_IN_MAIN', '1')
                logger.warning('[SUMMARY SCHEDULER STALE GUARD] main full mode: parent tick restored mode=%s', mode)

        if _suppress_fallback() or _main_skip_parent_tick():
            os.environ['ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED'] = '0'
            _reset_push_fallback_state(scheduler, 'install_suppress')

        _reset_unified_if_stale(scheduler, 'install')

        old_enabled = getattr(scheduler, '_push_fallback_when_blocked_enabled', None)
        if callable(old_enabled) and not getattr(old_enabled, '_stale_guard_patch_v5', False):
            def enabled_patched():
                if _suppress_fallback() or _main_skip_parent_tick():
                    logger.info('[SUMMARY SCHEDULER STALE GUARD] legacy PUSH fallback enabled? -> False')
                    return False
                try:
                    return bool(old_enabled())
                except Exception:
                    return False
            enabled_patched._stale_guard_patch_v5 = True  # type: ignore[attr-defined]
            enabled_patched._stale_guard_patch_v4 = True  # type: ignore[attr-defined]
            enabled_patched._stale_guard_patch_v3 = True  # type: ignore[attr-defined]
            enabled_patched._original = old_enabled  # type: ignore[attr-defined]
            scheduler._push_fallback_when_blocked_enabled = enabled_patched

        old_fallback = getattr(scheduler, '_run_push_fallback_when_unified_blocked', None)
        if callable(old_fallback) and not getattr(old_fallback, '_stale_guard_patch_v5', False):
            def fallback_patched(now, *, reason: str):
                reason_s = str(reason or '')
                if _suppress_fallback() or _main_skip_parent_tick():
                    _reset_push_fallback_state(scheduler, f'suppressed:{reason_s}')
                    try:
                        hhmm = f'{now.hour:02d}:{now.minute:02d}'
                    except Exception:
                        hhmm = str(now)
                    logger.warning(
                        '[SUMMARY SCHEDULER STALE GUARD] legacy PUSH fallback suppressed reason=%s hhmm=%s main_entry_only=%s parent_skip=%s',
                        reason_s, hhmm, _main_entry_only(), _main_skip_parent_tick(),
                    )
                    return None
                if (
                    _main_entry_only()
                    and reason_s == 'previous_unified_bg_still_running'
                    and _b('SUMMARY_SUPPRESS_PREVIOUS_UNIFIED_FALLBACK_IN_MAIN', True)
                ):
                    # ここでfallbackを起動すると、親tick timeout済みの古いworkerとfallback workerが
                    # 並列に増え、次tickも previous_unified_bg_still_running になりやすい。
                    # staleを超えていればunifiedフラグを解除し、fallbackは起動しない。
                    reset = _reset_unified_if_stale(scheduler, f'fallback_suppressed:{reason_s}')
                    _reset_push_fallback_state(scheduler, f'fallback_suppressed:{reason_s}')
                    logger.warning(
                        '[SUMMARY SCHEDULER STALE GUARD] previous unified fallback suppressed in main.py reason=%s reset_unified=%s',
                        reason_s, reset,
                    )
                    return None
                return old_fallback(now, reason=reason_s)
            fallback_patched._stale_guard_patch_v5 = True  # type: ignore[attr-defined]
            fallback_patched._stale_guard_patch_v4 = True  # type: ignore[attr-defined]
            fallback_patched._stale_guard_patch_v3 = True  # type: ignore[attr-defined]
            fallback_patched._original = old_fallback  # type: ignore[attr-defined]
            scheduler._run_push_fallback_when_unified_blocked = fallback_patched

        old_tick = getattr(scheduler, '_run_summary_tick', None)
        if callable(old_tick) and not getattr(old_tick, '_stale_guard_patch_v5', False):
            def tick_patched(now=None):
                _reset_unified_if_stale(scheduler, 'before_parent_tick')
                if _main_skip_parent_tick():
                    _reset_push_fallback_state(scheduler, 'main_parent_tick_skip')
                    logger.warning(
                        '[SUMMARY SCHEDULER STALE GUARD] main.py skip summary parent tick to avoid NAS DB/cache read. Set AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK=0 or AUTOSTOCK_MAIN_OPERATION_MODE=full to restore.'
                    )
                    return {'ok': True, 'skipped': True, 'reason': 'main_py_summary_parent_tick_skip'}
                if _suppress_fallback():
                    _reset_push_fallback_state(scheduler, 'before_parent_tick_suppress')
                return old_tick(now=now)
            tick_patched._stale_guard_patch_v5 = True  # type: ignore[attr-defined]
            tick_patched._stale_guard_patch_v4 = True  # type: ignore[attr-defined]
            tick_patched._stale_guard_patch_v3 = True  # type: ignore[attr-defined]
            tick_patched._original = old_tick  # type: ignore[attr-defined]
            scheduler._run_summary_tick = tick_patched

        _PATCHED = True
        logger.warning(
            '[SUMMARY SCHEDULER STALE GUARD] installed v5 suppress_fallback=%s main_entry_only=%s parent_skip=%s push_bg_all=%s mode=%s unified_stale=%.1f previous_fallback_suppress=%s',
            _suppress_fallback(), _main_entry_only(), _main_skip_parent_tick(), _push_bg_all(), _operation_mode(), _main_unified_stale_sec(), _b('SUMMARY_SUPPRESS_PREVIOUS_UNIFIED_FALLBACK_IN_MAIN', True),
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