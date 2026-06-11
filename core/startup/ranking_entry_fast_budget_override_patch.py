from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_INSTALLED = False
_LIGHT_INSTALLED = False


def _float_env(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == '' else float(v)
    except Exception:
        return float(default)


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


def _install_light_companion() -> bool:
    global _LIGHT_INSTALLED
    try:
        mod = __import__('core.startup.ranking_entry_budget_hard_stop_v6_patch', fromlist=['install'])
        fn = getattr(mod, 'install', None)
        ok = bool(fn()) if callable(fn) else False
        _LIGHT_INSTALLED = bool(ok)
        logger.warning('[RANKING ENTRY FAST BUDGET OVERRIDE] light companion installed=%s', ok)
        return bool(ok)
    except Exception:
        logger.exception('[RANKING ENTRY FAST BUDGET OVERRIDE] light companion install failed')
        return False


def _force_light_budget() -> None:
    os.environ['RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS'] = '12'
    os.environ['RANKING_ENTRY_FAST_MAX_SYMBOLS'] = '12'
    os.environ['RANKING_ENTRY_FAST_MAX_PER_SIDE'] = '8'
    os.environ['RANKING_ENTRY_FAST_MAX_PER_TYPE'] = '6'
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = '15'
    os.environ['RANKING_ENTRY_RUNTIME_WARN_SEC'] = '15'
    os.environ['RANKING_ENTRY_RUNTIME_STALE_SEC'] = '20'
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'] = '18'
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'] = '12'
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN'] = '3'
    os.environ['RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN'] = '3'
    os.environ['RANKING_ENTRY_SKIP_TECH_SAVE'] = '1'
    os.environ['RANKING_ENTRY_TECH_READONLY'] = '1'
    os.environ['RANKING_ENTRY_TECH_READ_BATCH_SIZE'] = '12'
    os.environ.setdefault('RANKING_ENTRY_LIGHT_MIN_SCORE', '50')
    os.environ.setdefault('RANKING_ENTRY_LIGHT_MIN_TURNOVER', '50000000')

    # 2026-06-11: main.py logs showed ranking build timing out before candidate
    # evaluation because the DB fallback read 1500 rows from multiple ranking
    # tables on the NAS.  Entry only needs a fresh snapshot head here; force the
    # source fallback to read the small snapshot tables only and cache them.
    os.environ['RANKING_ENTRY_SOURCE_DB_TABLES'] = 'ranking_snapshot_1min,ranking_snapshot'
    os.environ['RANKING_ENTRY_SOURCE_DB_MAX_ROWS'] = '300'
    os.environ['RANKING_ENTRY_SOURCE_DB_SCAN_ROWS'] = '300'
    os.environ['RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN'] = '10'
    os.environ['RANKING_ENTRY_SOURCE_DB_CACHE_TTL_SEC'] = '30'
    os.environ['RANKING_ENTRY_SOURCE_DB_SQLITE_TIMEOUT_SEC'] = '0.4'
    os.environ['RANKING_ENTRY_SOURCE_DB_BUSY_TIMEOUT_MS'] = '300'


def _apply_once() -> bool:
    raw_runtime = _float_env('RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC', 15.0)
    raw_build = _float_env('RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC', 18.0)
    raw_controller = _float_env('RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC', 12.0)
    runtime = max(5.0, min(float(raw_runtime), 15.0))
    build = max(6.0, min(float(raw_build), 18.0))
    controller = max(5.0, min(float(raw_controller), 12.0))
    lock_wait = max(1.0, min(_float_env('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC', 8.0), 10.0))
    os.environ['RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC'] = str(runtime)
    os.environ['RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC'] = str(build)
    os.environ['RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC'] = str(controller)
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = str(runtime)
    os.environ['RANKING_ENTRY_RUNTIME_WARN_SEC'] = str(runtime)
    os.environ['RANKING_ENTRY_RUNTIME_STALE_SEC'] = '20.0'
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'] = str(build)
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'] = str(controller)
    os.environ['RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN'] = '3'
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN'] = '3'
    os.environ['RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS'] = '12'
    os.environ['RANKING_ENTRY_FAST_MAX_SYMBOLS'] = '12'
    os.environ['RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS'] = '300'
    os.environ['SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC'] = str(lock_wait)
    os.environ.setdefault('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC', '0.25')
    os.environ['RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC'] = '10'
    os.environ['RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC'] = '30'
    os.environ.setdefault('RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE', '1')
    os.environ.setdefault('RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC', '300')
    _install_fast_stale_guard()
    _install_min_pending_timeout_rescue()
    _install_light_companion()
    _force_light_budget()
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
            logger.warning('[RANKING ENTRY FAST BUDGET OVERRIDE] enforce v16 i=%s/%s ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s rows=%s source_tables=%s source_scan=%s light=%s', i, loops, ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'), os.environ.get('RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS'), os.environ.get('RANKING_ENTRY_SOURCE_DB_TABLES'), os.environ.get('RANKING_ENTRY_SOURCE_DB_SCAN_ROWS'), _LIGHT_INSTALLED)
        time.sleep(sleep_sec)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return _apply_once()
    ok = _apply_once()
    threading.Thread(target=_watch_loop, name='ranking-entry-fast-budget-override', daemon=True).start()
    _INSTALLED = True
    logger.warning('[RANKING ENTRY FAST BUDGET OVERRIDE] installed v16 ok=%s runtime_budget=%s build_timeout=%s controller_timeout=%s max_pending=%s rows=%s source_tables=%s source_scan=%s light=%s watcher=True', ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'), os.environ.get('RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS'), os.environ.get('RANKING_ENTRY_SOURCE_DB_TABLES'), os.environ.get('RANKING_ENTRY_SOURCE_DB_SCAN_ROWS'), _LIGHT_INSTALLED)
    return True


try:
    install()
except Exception:
    logger.exception('[RANKING ENTRY FAST BUDGET OVERRIDE] auto install failed')
__all__ = ['install']