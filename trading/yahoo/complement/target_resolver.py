from __future__ import annotations
import datetime as dt, logging, time
from typing import Iterable
import pandas as pd
from trading.yahoo.download.download_runner import resolve_target_symbols
from .logging_utils import log_step
logger = logging.getLogger(__name__)
try:
    from trading.yahoo.symbol.yahoo_symbol_provider import get_yahoo_target_symbols
    _HAS_YAHOO_SYMBOL_PROVIDER = True
except Exception:
    _HAS_YAHOO_SYMBOL_PROVIDER = False
    def get_yahoo_target_symbols(*args, **kwargs): return []
try:
    from trading.ranking.runtime_symbols import normalize_symbols
except Exception:
    def normalize_symbols(symbols: Iterable[object]) -> set[str]:
        out = set()
        for s in symbols or []:
            if s is None: continue
            ss = str(s).strip()
            if ss.endswith(".0"): ss = ss[:-2]
            if ss: out.add(ss)
        return out

def sanitize_symbols(symbols: Iterable[object]) -> list[str]:
    out, seen = [], set()
    for s in symbols or []:
        if s is None: continue
        sym = str(s).strip()
        if sym.endswith(".0"): sym = sym[:-2]
        if sym and sym not in seen:
            seen.add(sym); out.append(sym)
    return out

def extract_success_symbols_from_df(df: pd.DataFrame) -> set[str]:
    if df is None or df.empty or "symbol" not in df.columns: return set()
    try: return normalize_symbols(df["symbol"].tolist())
    except Exception: return {str(s).strip() for s in df["symbol"].astype(str).tolist() if str(s).strip()}

def build_rows_by_symbol(df: pd.DataFrame) -> dict[str, int]:
    if df is None or df.empty or "symbol" not in df.columns: return {}
    try: return {str(k).strip(): int(v) for k, v in df.groupby("symbol", dropna=True).size().items() if str(k).strip()}
    except Exception: logger.exception("[YAHOO COMPLEMENT] build rows_by_symbol failed"); return {}

def build_last_bar_by_symbol(df: pd.DataFrame) -> dict[str, object]:
    if df is None or df.empty or "symbol" not in df.columns or "datetime" not in df.columns: return {}
    try:
        tmp = df.copy(); tmp["datetime"] = pd.to_datetime(tmp["datetime"], errors="coerce"); tmp = tmp.dropna(subset=["symbol", "datetime"])
        return {str(sym).strip(): g["datetime"].max() for sym, g in tmp.groupby("symbol", sort=False) if str(sym).strip() and not g.empty}
    except Exception: logger.exception("[YAHOO COMPLEMENT] build last_bar_by_symbol failed"); return {}

def resolve_cached_target_symbols(*, target_date: dt.date) -> list[str]:
    ts = time.time()
    try:
        symbols = sanitize_symbols(resolve_target_symbols(target_date=target_date, use_ranking_cache=True))
        logger.info("[YAHOO COMPLEMENT] resolved cached targets=%d target_date=%s sample=%s", len(symbols), target_date, symbols[:20])
        log_step("resolve_cached_target_symbols_done", ts, count=len(symbols)); return symbols
    except TypeError:
        symbols = sanitize_symbols(resolve_target_symbols(target_date=target_date))
        logger.warning("[YAHOO COMPLEMENT] fallback legacy resolve target_date=%s symbols=%d sample=%s", target_date, len(symbols), symbols[:20]); return symbols
    except Exception: logger.exception("[YAHOO COMPLEMENT] resolve cached targets failed"); return []

def resolve_all_ranking_symbols_for_reflect(*, target_date: dt.date) -> list[str]:
    ts = time.time(); symbols: list[str] = []
    if _HAS_YAHOO_SYMBOL_PROVIDER:
        try:
            symbols = sanitize_symbols(get_yahoo_target_symbols(max_symbols=None, include_today_all_rankings=True, target_date=target_date, include_active=False, include_light=False, include_universe=False))
        except Exception: logger.exception("[YAHOO REFLECT TARGET] provider failed"); symbols = []
    if not symbols:
        try: symbols = sanitize_symbols(resolve_target_symbols(target_date=target_date, use_ranking_cache=False))
        except Exception: logger.exception("[YAHOO REFLECT TARGET] fallback resolve_target_symbols failed"); symbols = []
    logger.info("[YAHOO REFLECT TARGET] all ranking symbols=%d target_date=%s source=ranking_raw_all_day status_filter=disabled sample=%s", len(symbols), target_date, symbols[:20])
    log_step("resolve_reflect_all_ranking_symbols_done", ts, count=len(symbols)); return symbols

def resolve_download_symbols_from_reflect_symbols(*, target_date: dt.date, reflect_symbols: Iterable[object]) -> list[str]:
    symbols = sanitize_symbols(reflect_symbols)
    logger.info("[YAHOO DOWNLOAD TARGET] symbols=%d target_date=%s derived_from=reflect_all_ranking status_filter=disabled sample=%s", len(symbols), target_date, symbols[:20])
    return symbols
__all__ = ["sanitize_symbols", "extract_success_symbols_from_df", "build_rows_by_symbol", "build_last_bar_by_symbol", "resolve_cached_target_symbols", "resolve_all_ranking_symbols_for_reflect", "resolve_download_symbols_from_reflect_symbols", "normalize_symbols"]
