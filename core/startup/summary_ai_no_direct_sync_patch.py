from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False

def apply_patch() -> bool:
    os.environ['SUMMARY_AI_ASYNC_ENTRY'] = '1'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC', '1')
    os.environ['SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'] = os.getenv('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC', '1')
    os.environ['SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS'] = os.getenv('SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS', '2')
    os.environ['SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC'] = os.getenv('SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC', '0.35')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY'] = '0'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX', '3')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_STALE_SEC'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_STALE_SEC', '90')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY'] = '1'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX', '8')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC', '2.0')
    try:
        from core.startup.summary_ai_async_direct_dispatch_patch import install as install_direct
        install_direct()
    except Exception:
        logger.debug('[SUMMARY AI NO DIRECT SYNC] direct dispatch install skipped', exc_info=True)
    return True

def watch():
    loops = max(1, min(int(float(os.getenv('SUMMARY_AI_NO_DIRECT_SYNC_WATCH_LOOPS', '8') or 8)), 30))
    sleep_sec = max(0.5, min(float(os.getenv('SUMMARY_AI_NO_DIRECT_SYNC_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = apply_patch()
        if i in (0, loops - 1):
            logger.warning('[SUMMARY AI NO DIRECT SYNC] enforce v3 i=%s/%s ok=%s direct_sync=%s direct_dispatch=%s', i, loops, ok, os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'), os.environ.get('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'))
        time.sleep(sleep_sec)

def install() -> bool:
    global _DONE
    if _DONE: return True
    ok = apply_patch()
    threading.Thread(target=watch, name='summary-ai-direct-sync-compat', daemon=True).start()
    _DONE = True
    logger.warning('[SUMMARY AI NO DIRECT SYNC] installed v3 ok=%s watcher=True direct_sync=%s direct_dispatch=%s', ok, os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'), os.environ.get('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'))
    return True
try: install()
except Exception: logger.exception('[SUMMARY AI NO DIRECT SYNC] auto install failed')
__all__ = ['install']
