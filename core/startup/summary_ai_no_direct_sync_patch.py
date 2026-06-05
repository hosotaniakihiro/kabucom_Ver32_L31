from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False

# Summary AI の承認候補を busy 時に捨てない。
# 発注コントローラが一時的に混雑しても、承認済み候補を短時間保持して再試行する。
def apply_patch() -> bool:
    os.environ['SUMMARY_AI_ASYNC_ENTRY'] = '1'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'] = '0'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY'] = '0'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX', '3')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_STALE_SEC'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_STALE_SEC', '90')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY'] = '1'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX', '8')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC', '2.0')
    return True

def watch():
    for i in range(240):
        ok = apply_patch()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning('[SUMMARY AI NO DIRECT SYNC] enforce ok=%s direct_sync=%s drop_busy=%s queue_max=%s stale=%s retry=%s retry_max=%s', ok, os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_STALE_SEC'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX'))
        time.sleep(0.5)

def install() -> bool:
    global _DONE
    if _DONE:
        return apply_patch()
    ok = apply_patch()
    threading.Thread(target=watch, name='summary-ai-no-direct-sync', daemon=True).start()
    _DONE = True
    logger.warning('[SUMMARY AI NO DIRECT SYNC] installed ok=%s watcher=True keep_busy_candidates=True', ok)
    return True
try:
    install()
except Exception:
    logger.exception('[SUMMARY AI NO DIRECT SYNC] auto install failed')
__all__ = ['install']
