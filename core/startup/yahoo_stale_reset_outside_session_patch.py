from __future__ import annotations
import datetime as dt, logging, threading, time
logger = logging.getLogger(__name__)
_DONE=False

def _in_session(now=None):
    now=now or dt.datetime.now(); t=now.time()
    return (dt.time(9,0) <= t <= dt.time(11,30)) or (dt.time(12,30) <= t <= dt.time(15,30))

def _reset_if_needed():
    try:
        import core.yahoo_tasks as y
        now=dt.datetime.now(); now_ts=time.time()
        running=bool(getattr(y,'_yahoo_running',False)); started=float(getattr(y,'_yahoo_started_at_epoch',0.0) or 0.0)
        elapsed=now_ts-started if running and started>0 else 0.0
        alive=False
        try:
            th=getattr(y,'_yahoo_worker_thread',None); alive=bool(th and th.is_alive())
        except Exception: alive=False
        if running and elapsed>300 and not _in_session(now):
            y._yahoo_running=False; y._yahoo_started_at_epoch=0.0; y._yahoo_worker_thread=None
            logger.warning('[YAHOO STALE RESET OUTSIDE SESSION] reset running=%s alive=%s elapsed=%.1fs now=%s', running, alive, elapsed, now.strftime('%Y-%m-%d %H:%M:%S'))
            return True
    except Exception:
        logger.exception('[YAHOO STALE RESET OUTSIDE SESSION] reset check failed')
    return False

def _patch_once():
    try:
        import core.yahoo_tasks as y
        cur=getattr(y,'_yahoo_wrapper',None)
        if not callable(cur): return False
        if getattr(cur,'_yahoo_stale_reset_outside_session_v1',False): return True
        orig=cur
        def patched():
            now=dt.datetime.now()
            if not _in_session(now):
                _reset_if_needed()
                logger.info('[YAHOO STALE RESET OUTSIDE SESSION] skip outside session now=%s', now.strftime('%Y-%m-%d %H:%M:%S'))
                return None
            return orig()
        patched._yahoo_stale_reset_outside_session_v1=True; patched._original=orig
        y._yahoo_wrapper=patched
        logger.warning('[YAHOO STALE RESET OUTSIDE SESSION] patched _yahoo_wrapper')
        return True
    except Exception:
        logger.exception('[YAHOO STALE RESET OUTSIDE SESSION] patch failed')
        return False

def _watch():
    for i in range(240):
        _reset_if_needed(); ok=_patch_once()
        if i in (0,1,5,15,30,60,120,239): logger.warning('[YAHOO STALE RESET OUTSIDE SESSION] enforce ok=%s', ok)
        time.sleep(0.5)

def install():
    global _DONE
    if _DONE: return _patch_once()
    ok=_patch_once(); threading.Thread(target=_watch,name='yahoo-stale-reset-outside-session',daemon=True).start(); _DONE=True
    logger.warning('[YAHOO STALE RESET OUTSIDE SESSION] installed ok=%s watcher=True', ok)
    return True
try:
    install()
except Exception:
    logger.exception('[YAHOO STALE RESET OUTSIDE SESSION] auto install failed')
__all__=['install']
