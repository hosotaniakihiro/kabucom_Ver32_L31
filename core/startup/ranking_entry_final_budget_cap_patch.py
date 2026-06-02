from __future__ import annotations
import functools, logging, os, threading, time
logger=logging.getLogger(__name__)
_DONE=False

def _cap_env():
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC']='25'
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC']='30'
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC']='30'
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN']='1'
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC=30.0
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC=30.0
    except Exception:
        pass

def _patch_efr():
    try:
        import trading.ranking.entry_from_ranking as efr
        for name in ('run_ranking_entry_pipeline','entry_from_ranking'):
            cur=getattr(efr,name,None)
            if not callable(cur) or getattr(cur,'_ranking_final_budget_cap_v1',False):
                continue
            @functools.wraps(cur)
            def wrapped(*args, __orig=cur, __name=name, **kwargs):
                _cap_env()
                logger.warning('[RANKING ENTRY FINAL BUDGET CAP] call %s runtime=%s build=%s controller=%s max_pending=%s', __name, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'))
                return __orig(*args, **kwargs)
            wrapped._ranking_final_budget_cap_v1=True
            wrapped._original=cur
            setattr(efr,name,wrapped)
        return True
    except Exception:
        logger.exception('[RANKING ENTRY FINAL BUDGET CAP] efr patch failed')
        return False

def _patch_tasks():
    try:
        import trading.entry_exit.tasks as tasks
        cur=getattr(tasks,'_dispatch_entry_controller',None)
        if callable(cur) and not getattr(cur,'_ranking_final_budget_cap_v1',False):
            @functools.wraps(cur)
            def wrapped_dispatch(*args, **kwargs):
                ps=str(kwargs.get('pipeline_source') or (args[0] if args else '') or '').upper()
                if ps=='RANKING':
                    old=kwargs.get('timeout_sec')
                    kwargs['timeout_sec']=min(float(old or 30.0),30.0)
                    _cap_env()
                    logger.warning('[RANKING ENTRY FINAL BUDGET CAP] controller timeout capped old=%s new=%s', old, kwargs.get('timeout_sec'))
                return cur(*args, **kwargs)
            wrapped_dispatch._ranking_final_budget_cap_v1=True
            wrapped_dispatch._original=cur
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
            logger.warning('[RANKING ENTRY FINAL BUDGET CAP] enforce ok=%s runtime=%s build=%s controller=%s max_pending=%s', ok, os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'), os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'), os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'))
        time.sleep(0.5)

def install():
    global _DONE
    if _DONE: return _apply()
    ok=_apply(); threading.Thread(target=_watch,name='ranking-entry-final-budget-cap',daemon=True).start(); _DONE=True
    logger.warning('[RANKING ENTRY FINAL BUDGET CAP] installed ok=%s watcher=True', ok)
    return True
try:
    install()
except Exception:
    logger.exception('[RANKING ENTRY FINAL BUDGET CAP] auto install failed')
__all__=['install']
