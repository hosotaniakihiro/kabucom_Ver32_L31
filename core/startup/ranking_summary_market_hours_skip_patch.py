from __future__ import annotations
import datetime as dt, logging, threading, time
logger=logging.getLogger(__name__)
_DONE=False

def _in_session(now=None):
    now=now or dt.datetime.now(); t=now.time()
    return (dt.time(9,0) <= t <= dt.time(11,30)) or (dt.time(12,30) <= t <= dt.time(15,30))

def _patch_once():
    try:
        import core.startup.scheduler_bootstrap as sb
        cur=getattr(sb,'_run_ranking_summary_all_job_safe',None)
        if not callable(cur): return False
        if getattr(cur,'_ranking_summary_market_hours_skip_v1',False): return True
        orig=cur
        def patched(*args, **kwargs):
            now=dt.datetime.now()
            if not _in_session(now):
                logger.warning('[RANKING SUMMARY MARKET HOURS SKIP] skip outside session now=%s', now.strftime('%Y-%m-%d %H:%M:%S'))
                return None
            return orig(*args, **kwargs)
        patched._ranking_summary_market_hours_skip_v1=True
        patched._original=orig
        sb._run_ranking_summary_all_job_safe=patched
        logger.warning('[RANKING SUMMARY MARKET HOURS SKIP] patched scheduler_bootstrap._run_ranking_summary_all_job_safe')
        return True
    except Exception:
        logger.exception('[RANKING SUMMARY MARKET HOURS SKIP] patch failed')
        return False

def _watch():
    for i in range(300):
        ok=_patch_once()
        if i in (0,1,5,15,30,60,120,240,299): logger.warning('[RANKING SUMMARY MARKET HOURS SKIP] enforce ok=%s', ok)
        time.sleep(0.5)

def install():
    global _DONE
    if _DONE: return _patch_once()
    ok=_patch_once(); threading.Thread(target=_watch,name='ranking-summary-market-hours-skip',daemon=True).start(); _DONE=True
    logger.warning('[RANKING SUMMARY MARKET HOURS SKIP] installed ok=%s watcher=True', ok)
    return True
try:
    install()
except Exception:
    logger.exception('[RANKING SUMMARY MARKET HOURS SKIP] auto install failed')
__all__=['install']
