from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False

def apply_patch() -> bool:
    os.environ['TONOSAMA_MAX_PENDING_PER_LOOP'] = '1'
    os.environ['TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES'] = os.getenv('TONOSAMA_ONE_PENDING_MAX_EVAL', '6')
    try:
        import trading.entry.tonosama.runner as runner
        old = getattr(runner, 'MAX_PENDING_PER_LOOP', None)
        runner.MAX_PENDING_PER_LOOP = 1
        logger.warning('[TONOSAMA ONE PENDING] apply old=%s new=%s max_eval=%s', old, getattr(runner, 'MAX_PENDING_PER_LOOP', None), os.environ.get('TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES'))
    except Exception:
        return False
    return True

def watch():
    for i in range(180):
        ok = apply_patch()
        if i in (0, 1, 5, 15, 30, 60, 120, 179):
            logger.warning('[TONOSAMA ONE PENDING] enforce ok=%s', ok)
        time.sleep(0.5)

def install() -> bool:
    global _DONE
    if _DONE:
        return apply_patch()
    ok = apply_patch()
    threading.Thread(target=watch, name='tonosama-one-pending', daemon=True).start()
    _DONE = True
    logger.warning('[TONOSAMA ONE PENDING] installed ok=%s watcher=True', ok)
    return True

try:
    install()
except Exception:
    logger.exception('[TONOSAMA ONE PENDING] auto install failed')
__all__ = ['install']
