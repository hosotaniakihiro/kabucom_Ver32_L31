from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False

def apply_cap() -> bool:
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = '25'
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'] = '30'
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'] = '30'
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN'] = '1'
    os.environ.setdefault('RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC', '45')
    os.environ.setdefault('RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC', '120')
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = 30.0
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = 30.0
    except Exception:
        return False
    return True

def watch():
    for i in range(240):
        ok = apply_cap()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning('[RANKING ENTRY INTRADAY CAP] enforce ok=%s runtime=%s build=%s controller=%s max_pending=%s', ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'))
        time.sleep(0.5)

def install() -> bool:
    global _DONE
    if _DONE:
        return apply_cap()
    ok = apply_cap()
    threading.Thread(target=watch, name='ranking-entry-intraday-cap', daemon=True).start()
    _DONE = True
    logger.warning('[RANKING ENTRY INTRADAY CAP] installed ok=%s watcher=True', ok)
    return True

try:
    install()
except Exception:
    logger.exception('[RANKING ENTRY INTRADAY CAP] auto install failed')
__all__ = ['install']
