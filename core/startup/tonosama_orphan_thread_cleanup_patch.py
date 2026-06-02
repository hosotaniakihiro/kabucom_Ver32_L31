from __future__ import annotations
import datetime as dt, functools, logging, os, threading, time
logger=logging.getLogger(__name__)
_DONE=False

def _pending_tonosama_count()->int:
    try:
        import trading.entry_exit.tasks as tasks
        fn=getattr(tasks,'_pending_count_for_source',None)
        if callable(fn): return int(fn('TONOSAMA') or 0)
    except Exception: pass
    return 0

def _cleanup(reason:str)->bool:
    try:
        import trading.entry_exit.tasks as tasks
        th=getattr(tasks,'_TONOSAMA_ENTRY_ORPHAN_THREAD',None)
        alive=bool(th is not None and getattr(th,'is_alive',lambda:False)())
        if th is None: return False
        pending=_pending_tonosama_count()
        cool=getattr(tasks,'_TONOSAMA_ENTRY_COOLDOWN_UNTIL',None)
        now=dt.datetime.now()
        cooldown_expired=(cool is None) or (isinstance(cool,dt.datetime) and now>=cool)
        force_empty=bool(int(float(os.getenv('TONOSAMA_ORPHAN_CLEANUP_IF_PENDING_EMPTY','1'))))
        force_cooldown=bool(int(float(os.getenv('TONOSAMA_ORPHAN_CLEANUP_IF_COOLDOWN_EXPIRED','1'))))
        do_clear=(force_empty and pending<=0) or (force_cooldown and cooldown_expired)
        if do_clear:
            tasks._TONOSAMA_ENTRY_ORPHAN_THREAD=None
            tasks._TONOSAMA_ENTRY_RUNNING=False
            tasks._TONOSAMA_ENTRY_STARTED_AT=None
            if pending<=0:
                tasks._TONOSAMA_ENTRY_TIMEOUT_STREAK=0
                tasks._TONOSAMA_ENTRY_COOLDOWN_UNTIL=None
            logger.warning('[TONOSAMA ORPHAN CLEANUP] cleared reason=%s alive=%s pending=%s cooldown=%s cooldown_expired=%s thread=%s', reason, alive, pending, cool, cooldown_expired, getattr(th,'name',None))
            return True
        logger.warning('[TONOSAMA ORPHAN CLEANUP] keep reason=%s alive=%s pending=%s cooldown=%s cooldown_expired=%s thread=%s', reason, alive, pending, cool, cooldown_expired, getattr(th,'name',None))
    except Exception:
        logger.exception('[TONOSAMA ORPHAN CLEANUP] cleanup failed reason=%s', reason)
    return False

def _patch_once()->bool:
    try:
        import trading.entry_exit.tasks as tasks
        cur=getattr(tasks,'_run_tonosama_entry_safe',None)
        if not callable(cur): return False
        if getattr(cur,'_tonosama_orphan_cleanup_v1',False): return True
        @functools.wraps(cur)
        def wrapped(*args,**kwargs):
            _cleanup('before_run')
            return cur(*args,**kwargs)
        wrapped._tonosama_orphan_cleanup_v1=True
        wrapped._original=cur
        tasks._run_tonosama_entry_safe=wrapped
        logger.warning('[TONOSAMA ORPHAN CLEANUP] patched _run_tonosama_entry_safe')
        return True
    except Exception:
        logger.exception('[TONOSAMA ORPHAN CLEANUP] patch failed')
        return False

def _watch():
    for i in range(600):
        _cleanup('watch')
        ok=_patch_once()
        if i in (0,1,5,15,30,60,120,240,480,599): logger.warning('[TONOSAMA ORPHAN CLEANUP] enforce ok=%s',ok)
        time.sleep(0.5)

def install()->bool:
    global _DONE
    if _DONE: return _patch_once()
    ok=_patch_once(); threading.Thread(target=_watch,name='tonosama-orphan-cleanup',daemon=True).start(); _DONE=True
    logger.warning('[TONOSAMA ORPHAN CLEANUP] installed ok=%s watcher=True',ok)
    return True
try:
    install()
except Exception:
    logger.exception('[TONOSAMA ORPHAN CLEANUP] auto install failed')
__all__=['install']
