from __future__ import annotations
import datetime as dt, logging, time
import pandas as pd
from utils.time_utils import get_yahoo_border_time
from trading.yahoo.storage.yahoo_1min_store import save_yahoo_1min
from trading.yahoo.diff.start_map_builder import build_periodic_symbol_start_map, build_startup_symbol_start_map
from trading.yahoo.download.download_runner import download_by_start_map
from .constants import YAHOO_SUMMARY_INTERVALS, YAHOO_REFLECT_DELAY_MINUTES
from .logging_utils import log_df_profile, log_step
from .runtime_cache import ensure_daily_cache_state
from .backfill_state import restore_db_backfilled_state, mark_download_result
from .target_resolver import resolve_all_ranking_symbols_for_reflect, resolve_download_symbols_from_reflect_symbols
from .time_window import resolve_yahoo_reflect_end_dt
from .dataframe_utils import normalize_downloaded_df, save_intraday_by_date
from .summary_reflector import reflect_saved_yahoo_to_summary_db
logger = logging.getLogger(__name__)

def run_periodic_yahoo_complement():
    job_ts = time.time()
    try:
        now = dt.datetime.now().replace(second=0, microsecond=0); border_time = get_yahoo_border_time(); end_dt = min(now, border_time); target_date = end_dt.date()
        reflect_end_dt = resolve_yahoo_reflect_end_dt(target_date=target_date, delay_minutes=YAHOO_REFLECT_DELAY_MINUTES)
        logger.info("[YAHOO COMPLEMENT] periodic start now=%s border_time=%s end_dt=%s reflect_end_dt=%s target_date=%s intervals=%s", now, border_time, end_dt, reflect_end_dt, target_date, YAHOO_SUMMARY_INTERVALS)
        ensure_daily_cache_state(target_date); restore_db_backfilled_state(target_date)
        reflect_symbols = resolve_all_ranking_symbols_for_reflect(target_date=target_date)
        download_symbols = resolve_download_symbols_from_reflect_symbols(target_date=target_date, reflect_symbols=reflect_symbols)
        if not reflect_symbols:
            logger.debug("Yahoo補完（定期）: ranking_raw当日銘柄なし → skip"); log_step("periodic_skip_no_reflect_symbols", job_ts); return None
        pre_result_map = reflect_saved_yahoo_to_summary_db(target_date=target_date, symbols=reflect_symbols, label="定期-pre-download-reflect-all")
        logger.info("[YAHOO COMPLEMENT] periodic_pre_download_reflect_done reflect_symbols=%d reflected=True result_keys=%s reflect_end_dt=%s", len(reflect_symbols), sorted(list(pre_result_map.keys())) if isinstance(pre_result_map, dict) else type(pre_result_map).__name__, reflect_end_dt)
        raw_df = pd.DataFrame()
        if download_symbols:
            raw_df = download_by_start_map(download_symbols, end_dt, build_periodic_symbol_start_map, target_date=target_date, log_prefix="YAHOO DIFF")
        if raw_df is None or raw_df.empty:
            reflect_saved_yahoo_to_summary_db(target_date=target_date, symbols=reflect_symbols, label="定期-empty-download-reflect-all")
            log_step("periodic_finish_empty_download_reflected_saved_all", job_ts, reflect_symbols=len(reflect_symbols), reflect_end_dt=reflect_end_dt); return None
        log_df_profile("periodic:raw_df", raw_df)
        df = normalize_downloaded_df(raw_df, label="Yahoo補完（定期）")
        if df.empty:
            mark_download_result(download_symbols, pd.DataFrame(), label="periodic-normalize-empty", target_date=target_date)
            reflect_saved_yahoo_to_summary_db(target_date=target_date, symbols=reflect_symbols, label="定期-normalize-empty-reflect-all"); return None
        try: save_yahoo_1min(df, target_date=target_date); logger.info("[YAHOO COMPLEMENT] periodic save_yahoo_1min done rows=%s target_date=%s", len(df), target_date)
        except Exception: logger.exception("❌ yahoo_1min 保存失敗（処理継続）")
        reflect_saved_yahoo_to_summary_db(target_date=target_date, symbols=reflect_symbols, label="定期-reflect-all")
        mark_download_result(download_symbols, df, label="periodic", target_date=target_date)
        log_step("periodic_total_done", job_ts, download_symbols=len(download_symbols), reflect_symbols=len(reflect_symbols), rows=len(df), reflect_end_dt=reflect_end_dt); return None
    except Exception: logger.exception("❌ Yahoo差分補完（定期）失敗（runtime 継続）"); return None

def run_startup_yahoo_complement():
    job_ts = time.time()
    try:
        today = dt.date.today(); end_dt = get_yahoo_border_time(); reflect_end_dt = resolve_yahoo_reflect_end_dt(target_date=today, delay_minutes=YAHOO_REFLECT_DELAY_MINUTES)
        logger.info("[YAHOO COMPLEMENT] startup start today=%s end_dt=%s reflect_end_dt=%s intervals=%s", today, end_dt, reflect_end_dt, YAHOO_SUMMARY_INTERVALS)
        ensure_daily_cache_state(today); restore_db_backfilled_state(today)
        reflect_symbols = resolve_all_ranking_symbols_for_reflect(target_date=today)
        download_symbols = resolve_download_symbols_from_reflect_symbols(target_date=today, reflect_symbols=reflect_symbols)
        if not reflect_symbols:
            logger.warning("Yahoo補完（起動時）: ranking_raw当日銘柄なし → skip"); log_step("startup_skip_no_reflect_symbols", job_ts); return True
        pre_result_map = reflect_saved_yahoo_to_summary_db(target_date=today, symbols=reflect_symbols, label="起動時-pre-download-reflect-all")
        logger.info("[YAHOO COMPLEMENT] startup_pre_download_reflect_done reflect_symbols=%d reflected=True result_keys=%s reflect_end_dt=%s", len(reflect_symbols), sorted(list(pre_result_map.keys())) if isinstance(pre_result_map, dict) else type(pre_result_map).__name__, reflect_end_dt)
        raw_df = pd.DataFrame()
        if download_symbols:
            raw_df = download_by_start_map(download_symbols, end_dt, build_startup_symbol_start_map, target_date=today, log_prefix="YAHOO STARTUP")
        if raw_df is None or raw_df.empty:
            reflect_saved_yahoo_to_summary_db(target_date=today, symbols=reflect_symbols, label="起動時-empty-download-reflect-all"); return True
        log_df_profile("startup:raw_df", raw_df)
        df = normalize_downloaded_df(raw_df, label="Yahoo補完（起動時）")
        if df.empty:
            mark_download_result(download_symbols, pd.DataFrame(), label="startup-normalize-empty", target_date=today)
            reflect_saved_yahoo_to_summary_db(target_date=today, symbols=reflect_symbols, label="起動時-normalize-empty-reflect-all"); return True
        save_intraday_by_date(df, fallback_date=today, label="Yahoo補完（起動時）")
        reflect_saved_yahoo_to_summary_db(target_date=today, symbols=reflect_symbols, label="起動時-reflect-all")
        mark_download_result(download_symbols, df, label="startup", target_date=today)
        log_step("startup_total_done", job_ts, download_symbols=len(download_symbols), reflect_symbols=len(reflect_symbols), rows=len(df), reflect_end_dt=reflect_end_dt); return True
    except Exception: logger.exception("❌ Yahoo補完（起動時）失敗 → 起動継続"); return True
__all__ = ["run_periodic_yahoo_complement", "run_startup_yahoo_complement"]
