from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False
_WATCHER_STARTED = False
_LAST_STATE = None

def apply_patch(log_change: bool = True) -> bool:
    global _LAST_STATE
    os.environ['TONOSAMA_MAX_PENDING_PER_LOOP'] = '1'
    os.environ['TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES'] = os.getenv('TONOSAMA_ONE_PENDING_MAX_EVAL', '6')
    try:
        import trading.entry.tonosama.runner as runner
        old = getattr(runner, 'MAX_PENDING_PER_LOOP', None)
        runner.MAX_PENDING_PER_LOOP = 1
        state = (str(getattr(runner, 'MAX_PENDING_PER_LOOP', None)), str(os.environ.get('TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES')))
        if log_change and state != _LAST_STATE:
            logger.warning('[TONOSAMA ONE PENDING] apply old=%s new=%s max_eval=%s', old, getattr(runner, 'MAX_PENDING_PER_LOOP', None), os.environ.get('TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES'))
        _LAST_STATE = state
    except Exception:
        return False
    return True

def watch():
    loops = max(1, min(int(float(os.getenv('TONOSAMA_ONE_PENDING_WATCH_LOOPS', '12') or 12)), 30))
    sleep_sec = max(0.5, min(float(os.getenv('TONOSAMA_ONE_PENDING_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = apply_patch(log_change=False)
        if i in (0, loops - 1):
            logger.warning('[TONOSAMA ONE PENDING] enforce v2 i=%s/%s ok=%s', i, loops, ok)
        time.sleep(sleep_sec)

def install() -> bool:
    global _DONE, _WATCHER_STARTED
    if _DONE and _WATCHER_STARTED:
        return True
    ok = apply_patch(log_change=True)
    if not _WATCHER_STARTED:
        threading.Thread(target=watch, name='tonosama-one-pending', daemon=True).start()
        _WATCHER_STARTED = True
    _DONE = True
    logger.warning('[TONOSAMA ONE PENDING] installed v2 ok=%s watcher=%s', ok, _WATCHER_STARTED)
    return True

try:
    install()
except Exception:
    logger.exception('[TONOSAMA ONE PENDING] auto install failed')
__all__ = ['install']
