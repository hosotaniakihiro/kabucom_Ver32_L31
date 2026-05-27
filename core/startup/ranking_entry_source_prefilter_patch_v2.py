from __future__ import annotations
import logging, os
import pandas as pd
logger = logging.getLogger(__name__)
_PATCHED=False
_ORIG=None

def _b(n,d=True):
    v=os.getenv(n)
    return d if v is None or str(v).strip()=='' else str(v).lower() in {'1','true','yes','on'}

def _i(n,d):
    try: return int(float(os.getenv(n,d)))
    except Exception: return d

def _f(n,d):
    try: return float(os.getenv(n,d))
    except Exception: return d

def _col(df,names):
    for n in names:
        if n in df.columns: return n
    return None

def _num(df,names,d=0.0):
    c=_col(df,names)
    return pd.to_numeric(df[c],errors='coerce').fillna(d) if c else pd.Series(d,index=df.index)

def _pf(df):
    if not isinstance(df,pd.DataFrame) or df.empty or not _b('RANKING_ENTRY_SOURCE_PREFILTER_ENABLED',True): return df
    if len(df)<=_i('RANKING_ENTRY_SOURCE_PREFILTER_SKIP_UNDER_ROWS',300): return df
    before=len(df)
    rank=_num(df,['rank_position','No','Rank','rank'],999999)
    price=_num(df,['price','current_price','CurrentPrice','close'],0)
    score=_num(df,['ranking_score_total','score','entry_score','total_score','final_score'],0)
    vol=_num(df,['volume','trading_volume','TradingVolume'],0)
    turn=_num(df,['turnover','trading_value','TradingValue'],0)
    minv=_f('RANKING_ENTRY_PREFILTER_MIN_VOLUME',15000)
    fixedv=vol.where(~((vol>0)&(vol<minv)),vol*1000.0)
    fixedt=turn.where(turn>=price*fixedv,price*fixedv)
    mask=((rank<=_i('RANKING_ENTRY_PREFILTER_MAX_RANK',25))|(score>=_f('RANKING_ENTRY_PREFILTER_MIN_SCORE',55)))
    mask&=(price>=_f('RANKING_ENTRY_PREFILTER_MIN_PRICE',300))&(price<=_f('RANKING_ENTRY_PREFILTER_MAX_PRICE',12000))
    mask&=((fixedv>=minv)|(fixedt>=_f('RANKING_ENTRY_PREFILTER_MIN_TURNOVER',5000000))|(rank<=_i('RANKING_ENTRY_PREFILTER_KEEP_RANK',20)))
    out=df.loc[mask].copy()
    if out.empty: return df
    out['_pf_rank']=rank.loc[out.index]
    out['_pf_score']=score.loc[out.index]
    out=out.sort_values(['_pf_score','_pf_rank'],ascending=[False,True])
    sc=_col(out,['symbol','Symbol','code','stock_code'])
    if sc: out=out.drop_duplicates(subset=[sc],keep='first')
    out=out.head(_i('RANKING_ENTRY_PREFILTER_MAX_ROWS',250)).drop(columns=['_pf_rank','_pf_score'],errors='ignore')
    logger.warning('[RANKING ENTRY PREFILTER] before=%s after=%s',before,len(out))
    return out.reset_index(drop=True)

def _getter(*a,**kw):
    df=_ORIG(*a,**kw) if callable(_ORIG) else None
    return _pf(df)

def install():
    global _PATCHED,_ORIG
    if _PATCHED: return True
    import trading.ranking.entry_from_ranking as efr
    cur=getattr(efr,'_get_ranking_source_df',None)
    if not callable(cur): return False
    if getattr(cur,'_ranking_entry_source_prefilter_v2',False):
        _PATCHED=True; return True
    _ORIG=cur
    _getter._ranking_entry_source_prefilter_v2=True
    efr._get_ranking_source_df=_getter
    _PATCHED=True
    logger.warning('[RANKING ENTRY PREFILTER] installed v2')
    return True
try: install()
except Exception: logger.exception('[RANKING ENTRY PREFILTER] auto install failed')
__all__=['install']
