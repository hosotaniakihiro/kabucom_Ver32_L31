from __future__ import annotations
import functools, logging, os, threading, time
logger=logging.getLogger(__name__)
_DONE=False

# Ranking は候補作成だけに時間を使い、entry_controller を長時間握らない。
# Summary AI の承認済み候補を優先するため、RANKING の controller timeout は短くする。
def _cap_env():
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC']='25'
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC']='30'
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC']='8'
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN']='4'
    os.environ['ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED']='0'
    os.environ['ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC']='0'
    os.environ.setdefault('ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC','75')
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC=30.0
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC=8.0
    except Exception:
        pass

def _patch_efr():
    try:
        import trading.ranking.entry_from_ranking as efr
        for name in ('run_ranking_entry_pipeline','entry_from_ranking'):
            cur=getattr(efr,name,None)
            if not callable(cur) or getattr(cur,'_ranking_final_budget_cap_v2',False):
                continue
            original=getattr(cur,'_original',cur)
            @functools.wraps(original)
            def wrapped(*args, __orig=original, __name=name, **kwargs):
                _cap_env()
                logger.warning('[RANKING ENTRY FINAL BUDGET CAP] call %s runtime=%s build=%s controller=%s max_pending=%s ranking_lock_wait=%s', __name, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'), os.environ.get('ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED'))
                return __orig(*args, **kwargs)
            wrapped._ranking_final_budget_cap_v1=True
            wrapped._ranking_final_budget_cap_v2=True
            wrapped._original=original
            setattr(efr,name,wrapped)
        return True
    except Exception:
        logger.exception('[RANKING ENTRY FINAL BUDGET CAP] efr patch failed')
        return False

def _patch_tasks():
    try:
        import trading.entry_exit.tasks as tasks
        cur=getattr(tasks,'_dispatch_entry_controller',None)
        if callable(cur) and not getattr(cur,'_ranking_final_budget_cap_v2',False):
            original=getattr(cur,'_original',cur)
            @functools.wraps(original)
            def wrapped_dispatch(*args, **kwargs):
                ps=str(kwargs.get('pipeline_source') or (args[0] if args else '') or '').upper()
                if ps=='RANKING':
                    old=kwargs.get('timeout_sec')
                    kwargs['timeout_sec']=min(float(old or 8.0),8.0)
                    _cap_env()
                    logger.warning('[RANKING ENTRY FINAL BUDGET CAP] controller timeout capped old=%s new=%s ranking_lock_wait=%s', old, kwargs.get('timeout_sec'), os.environ.get('ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED'))
                return original(*args, **kwargs)
            wrapped_dispatch._ranking_final_budget_cap_v1=True
            wrapped_dispatch._ranking_final_budget_cap_v2=True
            wrapped_dispatch._original=original
            tasks._dispatch_entry_controller=wrapped_dispatch
        _cap_env()
        return True
    except Exception:
        logger.exception('[RANKING ENTRY FINAL BUDGET CAP] tasks patch failed')
        return False

def _apply():
    _cap_env()
    return bool(_patch_efr() and _patch_tasks())

def _watch():
    for i in range(600):
        ok=_apply()
        if i in (0,1,5,15,30,60,120,240,480,599):
            logger.warning('[RANKING ENTRY FINAL BUDGET CAP] enforce ok=%s runtime=%s build=%s controller=%s max_pending=%s ranking_lock_wait=%s', ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'), os.environ.get('ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED'))
        time.sleep(0.5)

def install():
    global _DONE
    if _DONE: return _apply()
    ok=_apply(); threading.Thread(target=_watch,name='ranking-entry-final-budget-cap',daemon=True).start(); _DONE=True
    logger.warning('[RANKING ENTRY FINAL BUDGET CAP] installed v2 ok=%s watcher=True ranking_lock_wait=0 max_pending=4 controller=8', ok)
    return True
try:
    install()
except Exception:
    logger.exception('[RANKING ENTRY FINAL BUDGET CAP] auto install failed')
__all__=['install']
