from __future__ import annotations
import datetime as dt, logging, threading, time
logger=logging.getLogger(__name__)
_DONE=False

def _in_session(now=None):
    now=now or dt.datetime.now(); t=now.time()
    return (dt.time(9,0) <= t <= dt.time(11,30)) or (dt.time(12,30) <= t <= dt.time(15,30))

def _patch_once():
    try:
        import trading.entry_exit.tasks as tasks
        cur=getattr(tasks,'_run_tonosama_entry_safe',None)
        if not callable(cur): return False
        if getattr(cur,'_tonosama_market_hours_skip_v1',False): return True
        orig=cur
        def patched():
            now=dt.datetime.now()
            if not _in_session(now):
                logger.warning('[TONOSAMA MARKET HOURS SKIP] skip outside session now=%s', now.strftime('%Y-%m-%d %H:%M:%S'))
                return 0
            return orig()
        patched._tonosama_market_hours_skip_v1=True
        patched._original=orig
        tasks._run_tonosama_entry_safe=patched
        logger.warning('[TONOSAMA MARKET HOURS SKIP] patched _run_tonosama_entry_safe')
        return True
    except Exception:
        logger.exception('[TONOSAMA MARKET HOURS SKIP] patch failed')
        return False

def _watch():
    for i in range(300):
        ok=_patch_once()
        if i in (0,1,5,15,30,60,120,240,299): logger.warning('[TONOSAMA MARKET HOURS SKIP] enforce ok=%s', ok)
        time.sleep(0.5)

def install():
    global _DONE
    if _DONE: return _patch_once()
    ok=_patch_once(); threading.Thread(target=_watch,name='tonosama-market-hours-skip',daemon=True).start(); _DONE=True
    logger.warning('[TONOSAMA MARKET HOURS SKIP] installed ok=%s watcher=True', ok)
    return True
try:
    install()
except Exception:
    logger.exception('[TONOSAMA MARKET HOURS SKIP] auto install failed')
__all__=['install']
