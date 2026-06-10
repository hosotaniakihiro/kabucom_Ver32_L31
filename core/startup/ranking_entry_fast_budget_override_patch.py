from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_INSTALLED = False


def _float_env(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == '' else float(v)
    except Exception:
        return float(default)


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except Exception:
        x = float(lo)
    return max(float(lo), min(float(hi), x))


def _install_fast_stale_guard() -> bool:
    try:
        os.environ.setdefault('RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE', '1')
        os.environ.setdefault('RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC', '300')
        mod = __import__('core.startup.ranking_entry_fast_stale_snapshot_guard_patch', fromlist=['install'])
        fn = getattr(mod, 'install', None)
        return bool(fn()) if callable(fn) else False
    except Exception:
        logger.debug('[RANKING ENTRY FAST BUDGET OVERRIDE] fast stale guard install skipped', exc_info=True)
        return False


def _install_min_pending_timeout_rescue() -> bool:
    try:
        mod = __import__('core.startup.ranking_entry_min_pending_on_timeout_patch', fromlist=['install'])
        fn = getattr(mod, 'install', None)
        return bool(fn()) if callable(fn) else False
    except Exception:
        logger.debug('[RANKING ENTRY FAST BUDGET OVERRIDE] min pending rescue install skipped', exc_info=True)
        return False


def _apply_once() -> bool:
    # V12: align with ranking_stuck_pending_prune_patch v7.
    # V11 narrowed budgets to 15/18/12, which caused scoring/pending_add to stop
    # before a candidate could be added. Keep the fast path bounded, but allow
    # enough time for candidate scoring and controller dispatch.
    runtime = _clamp(_float_env('RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC', 25.0), 10.0, 25.0)
    build = _clamp(_float_env('RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC', 30.0), 10.0, 30.0)
    controller = _clamp(_float_env('RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC', 30.0), 10.0, 30.0)
    lock_wait = _clamp(_float_env('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC', 8.0), 1.0, 10.0)
    max_pending = str(int(_clamp(_float_env('RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN', 4.0), 1.0, 4.0)))
    os.environ['RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC'] = str(runtime)
    os.environ['RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC'] = str(build)
    os.environ['RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC'] = str(controller)
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = str(runtime)
    os.environ['RANKING_ENTRY_RUNTIME_WARN_SEC'] = str(runtime)
    os.environ['RANKING_ENTRY_RUNTIME_STALE_SEC'] = '35.0'
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'] = str(build)
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'] = str(controller)
    os.environ['RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN'] = max_pending
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN'] = max_pending
    os.environ['RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS'] = '24'
    os.environ['RANKING_ENTRY_FAST_MAX_SYMBOLS'] = '24'
    os.environ['RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS'] = '300'
    os.environ['SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC'] = str(lock_wait)
    os.environ.setdefault('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC', '0.25')
    os.environ['RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC'] = '10'
    os.environ['RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC'] = '30'
    os.environ.setdefault('RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE', '1')
    os.environ.setdefault('RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC', '300')
    _install_fast_stale_guard()
    _install_min_pending_timeout_rescue()
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'])
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'])
    except Exception:
        return False
    return True


def _watch_loop() -> None:
    loops = max(1, min(int(float(os.getenv('RANKING_ENTRY_FAST_BUDGET_WATCH_LOOPS', '12') or 12)), 60))
    sleep_sec = max(0.5, min(float(os.getenv('RANKING_ENTRY_FAST_BUDGET_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = _apply_once()
        if i in (0, loops - 1):
            logger.warning('[RANKING ENTRY FAST BUDGET OVERRIDE] enforce v12 i=%s/%s ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s', i, loops, ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'))
        time.sleep(sleep_sec)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return _apply_once()
    ok = _apply_once()
    threading.Thread(target=_watch_loop, name='ranking-entry-fast-budget-override', daemon=True).start()
    _INSTALLED = True
    logger.warning('[RANKING ENTRY FAST BUDGET OVERRIDE] installed v12 ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s watcher=True', ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'))
    return True


try:
    install()
except Exception:
    logger.exception('[RANKING ENTRY FAST BUDGET OVERRIDE] auto install failed')
__all__ = ['install']
