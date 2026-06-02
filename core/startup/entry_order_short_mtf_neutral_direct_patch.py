from __future__ import annotations
import logging, os
from typing import Any
logger=logging.getLogger(__name__)
_DONE=False

def _f(v:Any,d=0.0)->float:
    try:
        if v is None or str(v).strip()=="": return float(d)
        x=float(v); return x if x==x else float(d)
    except Exception: return float(d)

def _first(row:dict,*keys):
    for k in keys:
        try:
            v=row.get(k)
            if v is not None and str(v).strip()!="": return v
        except Exception: pass
    return None

def _score(row:dict,side:str)->float:
    side=str(side or '').upper()
    if side=='SELL':
        return max(_f(row.get('score_sell')), _f(row.get('sell_score')), abs(_f(row.get('score'))), abs(_f(row.get('final_score'))), abs(_f(row.get('display_score'))))
    return max(_f(row.get('score_buy')), _f(row.get('buy_score')), _f(row.get('score')), _f(row.get('final_score')), _f(row.get('display_score')))

def _can_skip_mtf_ng(row:dict, side:str, source:str, result:dict)->tuple[bool,dict]:
    if str(source or '').upper()!='SUMMARY_AI': return False, {'reason':'not_summary_ai','source':source}
    side=str(side or '').upper()
    if side not in ('BUY','SELL'): return False, {'reason':'bad_side','side':side}
    reason=str((result or {}).get('reason') or '').upper()
    if reason not in ('SHORT_MTF_NOT_BUY_ALIGNED','SHORT_MTF_NOT_SELL_ALIGNED','MTF_NOT_BUY_ALIGNED','MTF_NOT_SELL_ALIGNED'):
        return False, {'reason':'not_target','result_reason':reason}
    min_score=float(os.getenv('ENTRY_ORDER_SHORT_MTF_NEUTRAL_DIRECT_MIN_SCORE','1.0'))
    score=_score(row,side)
    if score<min_score: return False, {'reason':'score_low','score':score,'min_score':min_score}
    eps=abs(float(os.getenv('ENTRY_ORDER_SHORT_MTF_NEUTRAL_DIRECT_EPS','0.0')))
    s1=_f(_first(row,'slope_atr_scaled_1m','slope_1m','slope1m','slope_atr_scaled','slope','score_slope'))
    s3=_f(_first(row,'slope_atr_scaled_3m','slope_3m','slope3m'))
    s5=_f(_first(row,'slope_atr_scaled_5m','slope_5m','slope5m'))
    if side=='SELL':
        if not (s1 < -eps): return False, {'reason':'one_min_not_sell','slope_1m':s1,'eps':eps,'score':score}
        if s3 > eps or s5 > eps: return False, {'reason':'higher_tf_opposite','slope_3m':s3,'slope_5m':s5,'eps':eps,'score':score}
    else:
        if not (s1 > eps): return False, {'reason':'one_min_not_buy','slope_1m':s1,'eps':eps,'score':score}
        if s3 < -eps or s5 < -eps: return False, {'reason':'higher_tf_opposite','slope_3m':s3,'slope_5m':s5,'eps':eps,'score':score}
    return True, {'reason':'short_mtf_neutral_direct_pass','side':side,'score':score,'slopes':{'slope_1m':s1,'slope_3m':s3,'slope_5m':s5},'eps':eps,'original_reason':reason}

def install():
    global _DONE
    if _DONE: return True
    try:
        import trading.handlers.entry_order_builder as eob
        cur=getattr(eob,'_summary_mtf_direction_guard',None)
        if not callable(cur): return False
        if getattr(cur,'_short_mtf_neutral_direct_v1',False): _DONE=True; return True
        def patched(entry_row, *, symbol:str, side:str, source:str):
            res=cur(entry_row, symbol=symbol, side=side, source=source)
            try:
                if res is not None:
                    ok,detail=_can_skip_mtf_ng(entry_row or {}, side, source, res)
                    if ok:
                        logger.warning('[ENTRY ORDER SHORT MTF NEUTRAL DIRECT] pass symbol=%s side=%s detail=%s', symbol, side, detail)
                        return None
                    logger.info('[ENTRY ORDER SHORT MTF NEUTRAL DIRECT] keep_ng symbol=%s side=%s detail=%s res=%s', symbol, side, detail, res)
            except Exception:
                logger.exception('[ENTRY ORDER SHORT MTF NEUTRAL DIRECT] check failed')
            return res
        patched._short_mtf_neutral_direct_v1=True
        patched._original=cur
        eob._summary_mtf_direction_guard=patched
        _DONE=True
        logger.warning('[ENTRY ORDER SHORT MTF NEUTRAL DIRECT] installed v1')
        return True
    except Exception:
        logger.exception('[ENTRY ORDER SHORT MTF NEUTRAL DIRECT] install failed')
        return False
try:
    install()
except Exception:
    logger.exception('[ENTRY ORDER SHORT MTF NEUTRAL DIRECT] auto install failed')
__all__=['install']
