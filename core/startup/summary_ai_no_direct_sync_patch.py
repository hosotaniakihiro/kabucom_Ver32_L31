from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False

# Summary AI の承認候補を busy 時に捨てない。
# 発注コントローラが一時的に混雑しても、承認済み候補を短時間保持して再試行する。
# V2:
#   queued_async のまま実注文dispatchが薄い状態を避けるため、
#   summary_ai_async_direct_dispatch_patch と競合しないよう direct_sync を許可する。
#   ただし drop_busy=0 / retry=1 は維持して候補は捨てない。
def apply_patch() -> bool:
    os.environ['SUMMARY_AI_ASYNC_ENTRY'] = '1'
    # 旧: 常に '0'。これにより direct dispatch 保険と競合して queued_async に寄りやすかった。
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
    for i in range(240):
        ok = apply_patch()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning('[SUMMARY AI NO DIRECT SYNC] enforce ok=%s direct_sync=%s direct_dispatch=%s drop_busy=%s queue_max=%s stale=%s retry=%s retry_max=%s', ok, os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'), os.environ.get('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_STALE_SEC'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX'))
        time.sleep(0.5)

def install() -> bool:
    global _DONE
    if _DONE:
        return apply_patch()
    ok = apply_patch()
    threading.Thread(target=watch, name='summary-ai-direct-sync-compat', daemon=True).start()
    _DONE = True
    logger.warning('[SUMMARY AI NO DIRECT SYNC] installed ok=%s watcher=True keep_busy_candidates=True direct_sync=%s direct_dispatch=%s', ok, os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'), os.environ.get('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'))
    return True
try:
    install()
except Exception:
    logger.exception('[SUMMARY AI NO DIRECT SYNC] auto install failed')
__all__ = ['install']