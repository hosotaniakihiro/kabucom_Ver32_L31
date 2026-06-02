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
    for i in range(240):
        ok = apply_cap()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning(
                '[RANKING ENTRY INTRADAY CAP] enforce ok=%s runtime=%s build=%s controller=%s max_pending=%s summary_lock_wait=%s',
                ok,
                os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'),
                os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'),
                os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'),
                os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'),
                os.environ.get('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC'),
            )
        time.sleep(0.5)


def install() -> bool:
    global _DONE
    if _DONE:
        return apply_cap()
    ok = apply_cap()
    threading.Thread(target=watch, name='ranking-entry-intraday-cap', daemon=True).start()
    _DONE = True
    logger.warning(
        '[RANKING ENTRY INTRADAY CAP] installed v2 ok=%s runtime=%s build=%s controller=%s summary_lock_wait=%s watcher=True',
        ok,
        os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'),
        os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'),
        os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'),
        os.environ.get('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC'),
    )
    return True


try:
    install()
except Exception:
    logger.exception('[RANKING ENTRY INTRADAY CAP] auto install failed')
__all__ = ['install']
