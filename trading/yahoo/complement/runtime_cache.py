from __future__ import annotations
import datetime as dt, logging, time
from typing import Any
import pandas as pd
from .logging_utils import log_step
logger = logging.getLogger(__name__)
try:
    from core.global_context.context import global_data
except Exception:
    try: from global_state import global_data  # type: ignore
    except Exception: global_data = None
try:
    from trading.ranking.runtime_symbols import ensure_ranking_symbol_cache, clear_intraday_cache
    _HAS_RANKING_CACHE = True
except Exception:
    _HAS_RANKING_CACHE = False
    def ensure_ranking_symbol_cache(*args, **kwargs): return None
    def clear_intraday_cache(*args, **kwargs): return None

def safe_set_global_attr(name: str, value: Any) -> None:
    try:
        if global_data is not None: setattr(global_data, name, value)
    except Exception: logger.debug("[YAHOO COMPLEMENT] setattr failed name=%s", name, exc_info=True)

def safe_get_global_attr(name: str, default=None):
    try: return getattr(global_data, name, default) if global_data is not None else default
    except Exception: return default

def ensure_daily_cache_state(target_date: dt.date) -> None:
    ts = time.time()
    try:
        if not _HAS_RANKING_CACHE:
            logger.info("[YAHOO COMPLEMENT] runtime cache unavailable -> skip daily cache state"); return
        ensure_ranking_symbol_cache(target_date=target_date)
        current = target_date.strftime("%Y%m%d"); last = safe_get_global_attr("yahoo_cache_trade_date", None)
        if last != current:
            try: clear_intraday_cache(target_date=current)
            except Exception: logger.exception("[YAHOO COMPLEMENT] clear_intraday_cache failed")
            safe_set_global_attr("yahoo_cache_trade_date", current); logger.info("[YAHOO COMPLEMENT] intraday cache reset trade_date=%s prev=%s", current, last)
        else: logger.info("[YAHOO COMPLEMENT] intraday cache keep trade_date=%s", current)
        log_step("daily_cache_state_done", ts, target_date=target_date)
    except Exception: logger.exception("[YAHOO COMPLEMENT] ensure_daily_cache_state failed")

def latest_per_symbol_for_cache(df: pd.DataFrame, *, interval: int, label: str) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty: return pd.DataFrame()
    out = df.copy()
    if "symbol" not in out.columns:
        logger.warning("[YAHOO CACHE] skip no symbol column interval=%s label=%s", interval, label); return pd.DataFrame()
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce"); out = out.dropna(subset=["datetime"])
        if not out.empty: out = out.sort_values(["symbol", "datetime"]).groupby("symbol", as_index=False).tail(1)
    elif "date" in out.columns and "time" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"].astype(str) + " " + out["time"].astype(str), errors="coerce"); out = out.dropna(subset=["datetime"])
        if not out.empty: out = out.sort_values(["symbol", "datetime"]).groupby("symbol", as_index=False).tail(1)
    else: out = out.drop_duplicates(subset=["symbol"], keep="last")
    if out.empty: return pd.DataFrame()
    out["interval"] = int(interval); out["source"] = out.get("source", "yahoo")
    if "last_update" not in out.columns: out["last_update"] = pd.Timestamp.now()
    return out.reset_index(drop=True)

def update_runtime_df_cache_from_result_map(result_map: dict, *, label: str) -> None:
    if not isinstance(result_map, dict) or not result_map:
        logger.info("[YAHOO CACHE] skip empty result_map label=%s", label); return
    ts = time.time(); attempted = updated = 0
    try:
        try:
            from trading.yahoo.pipeline.complement.save import update_global_cache_if_possible, finalize_for_upsert_if_possible  # type: ignore
        except Exception:
            update_global_cache_if_possible = None; finalize_for_upsert_if_possible = None
        for interval, df in sorted(result_map.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999):
            if not isinstance(df, pd.DataFrame) or df.empty: continue
            try: interval_i = int(interval)
            except Exception: continue
            cache_df = latest_per_symbol_for_cache(df, interval=interval_i, label=label)
            if cache_df.empty: continue
            attempted += 1
            try:
                if finalize_for_upsert_if_possible is not None: cache_df = finalize_for_upsert_if_possible(cache_df, interval=interval_i)  # type: ignore[misc]
                if update_global_cache_if_possible is not None:
                    update_global_cache_if_possible(cache_df, interval=interval_i); updated += 1; continue  # type: ignore[misc]
                try: from core.global_context import global_data as gd  # type: ignore
                except Exception: gd = None
                if gd is not None and hasattr(gd, "set_merged_summary"): gd.set_merged_summary(interval_i, cache_df, source="yahoo"); updated += 1
                elif gd is not None and hasattr(gd, "set_push_merged_summary"): gd.set_push_merged_summary(interval_i, cache_df); updated += 1
                else: logger.warning("[YAHOO CACHE] no runtime cache backend interval=%s rows=%s label=%s", interval_i, len(cache_df), label)
            except Exception: logger.exception("[YAHOO CACHE] runtime df/cache update failed interval=%s label=%s", interval_i, label)
        logger.info("[YAHOO CACHE] runtime df/cache update done label=%s attempted=%s updated=%s", label, attempted, updated); log_step("yahoo_runtime_df_cache_update_done", ts, attempted=attempted, updated=updated)
    except Exception: logger.exception("[YAHOO CACHE] runtime df/cache update fatal label=%s", label)
__all__ = ["safe_set_global_attr", "safe_get_global_attr", "ensure_daily_cache_state", "latest_per_symbol_for_cache", "update_runtime_df_cache_from_result_map"]
