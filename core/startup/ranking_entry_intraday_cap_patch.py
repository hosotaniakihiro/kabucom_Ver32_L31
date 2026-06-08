from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False

def apply_cap() -> bool:
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = os.getenv('RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC', '150')
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'] = os.getenv('RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC', '180')
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'] = os.getenv('RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC', '120')
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN'] = os.getenv('RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN', '1')
    os.environ.setdefault('RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC', '90')
    os.environ.setdefault('RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC', '300')
    os.environ.setdefault('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC', '90')
    os.environ.setdefault('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC', '0.25')
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'])
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'])
    except Exception:
        return False
    return True

def watch():
    loops = max(1, min(int(float(os.getenv('RANKING_ENTRY_INTRADAY_CAP_WATCH_LOOPS', '8') or 8)), 30))
    sleep_sec = max(0.5, min(float(os.getenv('RANKING_ENTRY_INTRADAY_CAP_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = apply_cap()
        if i in (0, loops - 1):
            logger.warning('[RANKING ENTRY INTRADAY CAP] enforce v3 i=%s/%s ok=%s runtime=%s build=%s controller=%s max_pending=%s', i, loops, ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'))
        time.sleep(sleep_sec)

def install() -> bool:
    global _DONE
    if _DONE: return True
    ok = apply_cap()
    threading.Thread(target=watch, name='ranking-entry-intraday-cap', daemon=True).start()
    _DONE = True
    logger.warning('[RANKING ENTRY INTRADAY CAP] installed v3 ok=%s runtime=%s build=%s controller=%s watcher=True', ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'))
    return True
try: install()
except Exception: logger.exception('[RANKING ENTRY INTRADAY CAP] auto install failed')
__all__ = ['install']
