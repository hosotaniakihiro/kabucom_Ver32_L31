from __future__ import annotations
import datetime as dt, logging, os, time
import pandas as pd
from utils.time_utils import get_yahoo_border_time
from trading.yahoo.storage.yahoo_1min_store import save_yahoo_1min
from trading.yahoo.diff.start_map_builder import build_periodic_symbol_start_map, build_startup_symbol_start_map
from trading.yahoo.download.download_runner import download_by_start_map
from .constants import YAHOO_SUMMARY_INTERVALS, YAHOO_REFLECT_DELAY_MINUTES
from .logging_utils import log_df_profile, log_step, log_pipeline_result
from .runtime_cache import ensure_daily_cache_state
from .backfill_state import restore_db_backfilled_state, mark_download_result
from .target_resolver import resolve_all_ranking_symbols_for_reflect, resolve_download_symbols_from_reflect_symbols
from .time_window import resolve_yahoo_reflect_end_dt
from .dataframe_utils import normalize_downloaded_df, save_intraday_by_date
from .summary_reflector import reflect_saved_yahoo_to_summary_db
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(str(os.getenv(name, str(default))).strip())
        if min_value is not None:
            v = max(v, min_value)
        if max_value is not None:
            v = min(v, max_value)
        return v
    except Exception:
        return default


def _limit_symbols(symbols, *, limit: int):
    try:
        items = list(symbols or [])
    except Exception:
        return symbols
    if limit <= 0 or len(items) <= limit:
        return items
    logger.warning("[YAHOO MAIN CACHE] symbol limited before=%s after=%s", len(items), limit)
    return items[:limit]


def run_periodic_yahoo_complement_main_cache_only():
    """
    main.py 専用の軽量Yahoo即時利用モード。

    目的:
      - Yahooから不足分を取得して 1m/3m/5m summary を計算する
      - global cache / controller cache に即時反映し、entry判定で使えるようにする
      - NAS summary DB upsert / yahoo_1min保存 / reflect_saved_yahoo_to_summary_db は実行しない

    main_database.py 側の保存補完とは役割を分離する。
    """
    job_ts = time.time()
    try:
        now = dt.datetime.now().replace(second=0, microsecond=0)
        border_time = get_yahoo_border_time()
        end_dt = min(now, border_time)
        target_date = end_dt.date()
        max_symbols = _env_int("AUTOSTOCK_MAIN_YAHOO_MEMORY_MAX_SYMBOLS", 80, min_value=1, max_value=500)
        logger.info(
            "[YAHOO MAIN CACHE] start now=%s border_time=%s end_dt=%s target_date=%s intervals=%s max_symbols=%s save_db=0 update_cache=1",
            now,
            border_time,
            end_dt,
            target_date,
            YAHOO_SUMMARY_INTERVALS,
            max_symbols,
        )

        # ranking DB / state の軽い読込は許可。重い summary DB 保存はしない。
        try:
            ensure_daily_cache_state(target_date)
        except Exception:
            logger.debug("[YAHOO MAIN CACHE] ensure_daily_cache_state failed; continue", exc_info=True)
        try:
            restore_db_backfilled_state(target_date)
        except Exception:
            logger.debug("[YAHOO MAIN CACHE] restore_db_backfilled_state failed; continue", exc_info=True)

        reflect_symbols = resolve_all_ranking_symbols_for_reflect(target_date=target_date)
        if not reflect_symbols:
            logger.info("[YAHOO MAIN CACHE] no ranking symbols -> skip")
            log_step("main_cache_skip_no_reflect_symbols", job_ts)
            return None

        reflect_symbols = _limit_symbols(reflect_symbols, limit=max_symbols)
        download_symbols = resolve_download_symbols_from_reflect_symbols(target_date=target_date, reflect_symbols=reflect_symbols)
        download_symbols = _limit_symbols(download_symbols, limit=max_symbols)
        if not download_symbols:
            logger.info("[YAHOO MAIN CACHE] no download symbols -> skip reflect_symbols=%s", len(reflect_symbols))
            log_step("main_cache_skip_no_download_symbols", job_ts, reflect_symbols=len(reflect_symbols))
            return None

        raw_df = download_by_start_map(
            download_symbols,
            end_dt,
            build_periodic_symbol_start_map,
            target_date=target_date,
            log_prefix="YAHOO MAIN CACHE",
        )
        if raw_df is None or raw_df.empty:
            logger.info("[YAHOO MAIN CACHE] empty download reflect_symbols=%s download_symbols=%s", len(reflect_symbols), len(download_symbols))
            log_step("main_cache_finish_empty_download", job_ts, reflect_symbols=len(reflect_symbols), download_symbols=len(download_symbols))
            return None

        log_df_profile("main_cache:raw_df", raw_df)
        df = normalize_downloaded_df(raw_df, label="Yahoo補完（main cache only）")
        if df.empty:
            logger.info("[YAHOO MAIN CACHE] normalize empty rows_raw=%s", len(raw_df))
            log_step("main_cache_normalize_empty", job_ts, rows_raw=len(raw_df))
            return None

        from trading.yahoo.pipeline.complement_pipeline import run_yahoo_complement_pipeline

        result_map = run_yahoo_complement_pipeline(
            df,
            intervals=YAHOO_SUMMARY_INTERVALS,
            save=False,
            update_cache=True,
            date_yyyymmdd=target_date.strftime("%Y%m%d"),
            now=now,
        )
        log_pipeline_result(result_map, label="[YAHOO MAIN CACHE]")
        log_step(
            "main_cache_total_done",
            job_ts,
            download_symbols=len(download_symbols),
            reflect_symbols=len(reflect_symbols),
            rows=len(df),
            save_db=0,
            update_cache=1,
        )
        return result_map
    except Exception:
        logger.exception("❌ Yahoo main cache complement failed（runtime 継続）")
        return None


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
        logger.info("[YAHOO COMPLEMENT] startup_pre_download_reflect_done reflect_symbols=%d reflected=True result_keys=%s", len(reflect_symbols), sorted(list(pre_result_map.keys())) if isinstance(pre_result_map, dict) else type(pre_result_map).__name__)
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


__all__ = ["run_periodic_yahoo_complement", "run_periodic_yahoo_complement_main_cache_only", "run_startup_yahoo_complement"]
