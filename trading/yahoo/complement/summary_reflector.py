from __future__ import annotations
import datetime as dt, logging, time
from typing import Iterable
import pandas as pd
from trading.yahoo.pipeline.complement_pipeline import run_yahoo_mtf_summary_pipeline
from .constants import YAHOO_SUMMARY_INTERVALS, YAHOO_REFLECT_DELAY_MINUTES
from .logging_utils import log_df_profile, log_step, log_pipeline_result
from .runtime_cache import update_runtime_df_cache_from_result_map
from .time_window import resolve_yahoo_reflect_end_dt
from .yahoo_saved_loader import load_saved_yahoo_1min_for_summary
logger = logging.getLogger(__name__)

def run_summary_pipeline(df: pd.DataFrame, *, label: str) -> dict:
    if df is None or df.empty: return {}
    ts = time.time()
    try:
        result_map = run_yahoo_mtf_summary_pipeline(df, intervals=YAHOO_SUMMARY_INTERVALS)
        if not isinstance(result_map, dict):
            logger.warning("%s: summary pipeline returned non-dict", label); return {}
        logger.info("[YAHOO COMPLEMENT] %s summary pipeline returned keys=%s", label, list(result_map.keys()))
        for k, v in result_map.items():
            if isinstance(v, pd.DataFrame): log_df_profile(f"{label}:summary_interval:{k}", v)
        update_runtime_df_cache_from_result_map(result_map, label=label)
        log_step(f"{label}:summary_pipeline_done", ts); return result_map
    except Exception: logger.exception("❌ Yahoo MTF summary pipeline failed（%s）", label); return {}

def reflect_saved_yahoo_to_summary_db(*, target_date: dt.date, symbols: Iterable[object] | None, label: str) -> dict:
    ts = time.time(); reflect_end_dt = resolve_yahoo_reflect_end_dt(target_date=target_date, delay_minutes=YAHOO_REFLECT_DELAY_MINUTES)
    logger.info("[YAHOO COMPLEMENT] %s reflect_saved_yahoo_to_summary start target_date=%s reflect_end_dt=%s", label, target_date, reflect_end_dt)
    df_saved = load_saved_yahoo_1min_for_summary(target_date=target_date, symbols=symbols, end_dt=reflect_end_dt, reason=label)
    if df_saved.empty:
        log_step(f"{label}:reflect_saved_skip_empty", ts, reflect_end_dt=reflect_end_dt); return {}
    log_df_profile(f"{label}:saved_yahoo_1min_for_summary", df_saved)
    result_map = run_summary_pipeline(df_saved, label=f"{label}:saved_yahoo_cutoff")
    log_pipeline_result(result_map, label=f"[YAHOO MTF] {label}:saved_yahoo_cutoff")
    log_step(f"{label}:reflect_saved_yahoo_to_summary_done", ts, rows=len(df_saved), symbols=df_saved["symbol"].nunique() if "symbol" in df_saved.columns else 0, reflect_end_dt=reflect_end_dt)
    return result_map
__all__ = ["run_summary_pipeline", "reflect_saved_yahoo_to_summary_db"]
