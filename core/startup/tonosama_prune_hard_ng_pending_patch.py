from __future__ import annotations
import functools, logging, os
from typing import Any
logger=logging.getLogger(__name__)
_DONE=False
HARD_REASONS={s.strip().upper() for s in os.getenv('TONOSAMA_PRUNE_HARD_NG_REASONS','ATR_1M_FILTER_NG,RANGE_5M_FILTER_NG,SELL_CREDIT_GUARD_NG,POSITION_FILTER_NG,ENTRY_QTY_ZERO,LOW_MOVE_ATR_TOO_SMALL,LOW_MOVE_RANGE_TOO_SMALL').split(',') if s.strip()}

def _is_tonosama(entry:Any)->bool:
    try:
        if not isinstance(entry,dict): return False
        return str(entry.get('source') or '').upper()=='TONOSAMA' or str(entry.get('entry_type') or '').upper()=='TONOSAMA'
    except Exception: return False

def _prune(symbol:str, reason:str)->int:
    try:
        from trading.entry import pending_manager as pm
        def pred(sym, entry):
            return str(sym)==str(symbol) and _is_tonosama(entry)
        n=pm.prune_entries(pred, reason='TONOSAMA_HARD_NG_'+str(reason), max_remove=None)
        if n:
            logger.warning('[TONOSAMA PRUNE HARD NG PENDING] pruned symbol=%s reason=%s count=%s', symbol, reason, n)
        return int(n or 0)
    except Exception:
        logger.exception('[TONOSAMA PRUNE HARD NG PENDING] prune failed symbol=%s reason=%s', symbol, reason)
        return 0

def install():
    global _DONE
    if _DONE: return True
    try:
        import trading.handlers.entry_controller as ec
        cur=getattr(ec,'_log_skip',None)
        if not callable(cur): return False
        if getattr(cur,'_tonosama_prune_hard_ng_v1',False): _DONE=True; return True
        @functools.wraps(cur)
        def wrapped(symbol:str, reason:str, **detail):
            ret=cur(symbol, reason, **detail)
            try:
                r=str(reason or '').upper()
                if r in HARD_REASONS:
                    side=str(detail.get('side') or '').upper()
                    # symbol単位でTONOSAMA pendingだけ消す。SUMMARY/RANKING pendingは対象外。
                    _prune(str(symbol), r)
                    logger.warning('[TONOSAMA PRUNE HARD NG PENDING] hard_ng symbol=%s side=%s reason=%s detail=%s', symbol, side, r, detail)
            except Exception:
                logger.exception('[TONOSAMA PRUNE HARD NG PENDING] wrapper check failed')
            return ret
        wrapped._tonosama_prune_hard_ng_v1=True
        wrapped._original=cur
        ec._log_skip=wrapped
        _DONE=True
        logger.warning('[TONOSAMA PRUNE HARD NG PENDING] installed v1 reasons=%s', sorted(HARD_REASONS))
        return True
    except Exception:
        logger.exception('[TONOSAMA PRUNE HARD NG PENDING] install failed')
        return False
try:
    install()
except Exception:
    logger.exception('[TONOSAMA PRUNE HARD NG PENDING] auto install failed')
__all__=['install']
