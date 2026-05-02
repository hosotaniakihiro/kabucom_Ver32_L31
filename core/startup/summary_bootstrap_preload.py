# ============================================================
# File   : core/startup/summary_bootstrap_preload.py
# Ver    : PRODUCTION-STABLE-REV13.1-SUMMARY-BOOTSTRAP-PRELOAD
# ------------------------------------------------------------
# 【概要】
#   summary bootstrap 用 preload / ranking union 群
#
# 【主な機能】
#   - recent preload
#   - initial preload 呼び出し
#   - ranking strict union symbol 抽出
#   - multi-day preload
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import List, Optional, Set

import pandas as pd
from sqlalchemy import text

from global_state import global_data
from database.session import get_ranking_engine
from trading.summary.persistence.summary_loader import (
    load_recent_1min,
    load_recent_3min,
    load_recent_5min,
    load_multi_day_summary,
)
from trading.summary.recovery.helpers import drop_rows_outside_allowed_dates
from utils.business_day_utils import get_previous_business_day, is_today_business_day

from .summary_bootstrap_helpers import (
    safe_symbol_series,
    safe_symbol_nunique,
    log_boot_df,
    normalize_summary_frame,
    ensure_summary_display_columns,
    persist_summary_df_to_db,
    backfill_symbolname,
    apply_market_filter_df,
    filter_symbol_list,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVALS = (1, 3, 5)

_MIN_MULTI_DAY_SYMBOL_COVERAGE_RATIO = {
    1: 0.10,
    3: 0.25,
    5: 0.25,
}
_MIN_MULTI_DAY_SYMBOL_COVERAGE_ABS = {
    1: 50,
    3: 100,
    5: 100,
}


def safe_import_indicator_calculator():
    try:
        from trading.summary.indicators.indicator_calculator import add_all_indicators
        return add_all_indicators
    except Exception:
        logger.exception("indicator_calculator import failed")
        return None


def safe_import_scoring_pipeline():
    try:
        from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
        return run_scoring_pipeline
    except Exception:
        logger.exception("scoring_pipeline import failed")
        return None


def safe_import_preload_initial_summary():
    try:
        from trading.summary.initial_summary_preload import preload_initial_summary
        return preload_initial_summary
    except Exception:
        logger.exception("preload_initial_summary import failed")
        return None


def early_process_preload_df(df: pd.DataFrame, tf: int, source_stage: str) -> pd.DataFrame:
    out = normalize_summary_frame(df, tf=tf)
    if out.empty:
        return out

    out = backfill_symbolname(out)
    out = ensure_summary_display_columns(out)
    log_boot_df(f"{source_stage}_normalized", tf, out)

    add_all_indicators = safe_import_indicator_calculator()
    if callable(add_all_indicators):
        try:
            df_ind = add_all_indicators(out.copy(), interval=int(tf))
            if isinstance(df_ind, pd.DataFrame) and not df_ind.empty:
                out = normalize_summary_frame(df_ind, tf=tf)
                out = backfill_symbolname(out)
                logger.info("✅ [%smin/%s] indicators applied rows=%d", tf, source_stage, len(out))
                log_boot_df(f"{source_stage}_post_indicators", tf, out)
        except TypeError:
            try:
                df_ind = add_all_indicators(out.copy())
                if isinstance(df_ind, pd.DataFrame) and not df_ind.empty:
                    out = normalize_summary_frame(df_ind, tf=tf)
                    out = backfill_symbolname(out)
            except Exception:
                logger.exception("indicator apply failed tf=%s stage=%s", tf, source_stage)
        except Exception:
            logger.exception("indicator apply failed tf=%s stage=%s", tf, source_stage)

    run_scoring_pipeline = safe_import_scoring_pipeline()
    interval_label = f"{int(tf)}min"
    if callable(run_scoring_pipeline):
        try:
            try:
                df_score = run_scoring_pipeline(out.copy(), interval=interval_label)
            except TypeError:
                try:
                    df_score = run_scoring_pipeline(out.copy(), interval_label)
                except TypeError:
                    df_score = run_scoring_pipeline(out.copy())

            if isinstance(df_score, pd.DataFrame) and not df_score.empty:
                out = normalize_summary_frame(df_score, tf=tf)
                out = backfill_symbolname(out)
                log_boot_df(f"{source_stage}_post_scoring_raw", tf, out)
                out = ensure_summary_display_columns(out)
                logger.info("✅ [%smin/%s] scoring applied rows=%d", tf, source_stage, len(out))
                log_boot_df(f"{source_stage}_post_scoring", tf, out)
        except Exception:
            logger.exception("scoring apply failed tf=%s stage=%s", tf, source_stage)

    out = ensure_summary_display_columns(out)
    log_boot_df(f"{source_stage}_post_display_columns", tf, out)
    return out


def get_recent_preload_df(tf: int) -> pd.DataFrame:
    try:
        latest_map = getattr(global_data, "latest_summary_by_interval", None)
        if isinstance(latest_map, dict):
            df = latest_map.get(tf)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df.copy(deep=True)
    except Exception:
        logger.debug("recent preload map access failed tf=%s", tf, exc_info=True)

    try:
        df = global_data.get_merged_summary(tf)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df.copy(deep=True)
    except Exception:
        logger.debug("get_merged_summary fallback failed tf=%s", tf, exc_info=True)

    return pd.DataFrame()


def preload_recent_summaries() -> None:
    global_data.latest_summary_by_interval = {}

    for tf, loader in ((1, load_recent_1min), (3, load_recent_3min), (5, load_recent_5min)):
        try:
            df = loader()
            df = drop_rows_outside_allowed_dates(
                df,
                label="recent_preload_raw",
                include_previous_business_day=True,
                interval=int(tf),
            )

            if isinstance(df, pd.DataFrame) and not df.empty:
                log_boot_df("recent_raw", tf, df)
                df = normalize_summary_frame(df, tf=tf)
                df = backfill_symbolname(df)
                log_boot_df("recent_normalized", tf, df)
                df = apply_market_filter_df(df)
                log_boot_df("recent_filtered", tf, df)

                if not df.empty:
                    processed = early_process_preload_df(df, tf, "recent")
                    safe_df = processed.copy(deep=True) if isinstance(processed, pd.DataFrame) and not processed.empty else ensure_summary_display_columns(normalize_summary_frame(df, tf=tf)).copy(deep=True)

                    global_data.latest_summary_by_interval[tf] = safe_df.copy(deep=True)
                    global_data.set_merged_summary(tf, safe_df.copy(deep=True))

                    try:
                        if tf == 1:
                            global_data.latest_summary_1m = safe_df.copy(deep=True)
                        elif tf == 3:
                            global_data.latest_summary_3m = safe_df.copy(deep=True)
                        elif tf == 5:
                            global_data.latest_summary_5m = safe_df.copy(deep=True)
                    except Exception:
                        logger.debug("latest_summary attr set failed tf=%s", tf, exc_info=True)

                    persist_summary_df_to_db(safe_df, tf, stage="recent_preload")
                    logger.info("📈 recent preload %smin rows=%d", tf, len(safe_df))
                else:
                    logger.warning("⚠ recent preload filtered empty %smin", tf)
            else:
                logger.warning("⚠ recent preload empty %smin", tf)

        except Exception:
            logger.exception("recent preload failed tf=%s", tf)


def run_initial_summary_preload_if_available() -> None:
    fn = safe_import_preload_initial_summary()
    if not callable(fn):
        logger.warning("⚠ preload_initial_summary unavailable")
        return

    try:
        result = fn()
        logger.info("✅ preload_initial_summary executed result=%s", result)
    except Exception:
        logger.exception("preload_initial_summary execution failed")


def resolve_ranking_db_path_for_date(target_date: dt.date) -> Optional[str]:
    ymd = target_date.strftime("%Y%m%d")
    candidates = [
        rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking{ymd}.db",
        rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\Ranking\ranking{ymd}.db",
    ]

    logger.info("🔎 ranking DB candidates for %s -> %s", target_date, candidates)
    for path in candidates:
        try:
            exists = os.path.exists(path)
            logger.info("   - exists=%s path=%s", exists, path)
            if exists:
                logger.info("✅ ranking DB resolved for %s -> %s", target_date, path)
                return path
        except Exception:
            logger.exception("ranking db exists check failed path=%s", path)

    logger.warning("⚠ ranking DB not found for %s", target_date)
    return None


def fetch_table_columns(conn, table_name: str) -> List[str]:
    try:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        return [str(r[1]) for r in rows if len(r) > 1 and r[1] is not None]
    except Exception:
        logger.debug("ranking table column fetch failed table=%s", table_name, exc_info=True)
        return []


def build_ranking_symbol_queries(table_name: str, columns: List[str]) -> List[tuple[str, str]]:
    colset = {str(c) for c in columns}
    if "symbol" not in colset:
        return []

    queries: List[tuple[str, str]] = []

    if "date" in colset:
        queries.append(("date_ymd", f"SELECT DISTINCT symbol FROM {table_name} WHERE CAST(date AS TEXT) = :target_ymd"))
        queries.append(("date_compact", f"SELECT DISTINCT symbol FROM {table_name} WHERE REPLACE(CAST(date AS TEXT), '-', '') = :target_compact"))

    if "date_str" in colset:
        queries.append(("date_str_ymd", f"SELECT DISTINCT symbol FROM {table_name} WHERE CAST(date_str AS TEXT) = :target_ymd"))
        queries.append(("date_str_compact", f"SELECT DISTINCT symbol FROM {table_name} WHERE REPLACE(CAST(date_str AS TEXT), '-', '') = :target_compact"))

    for dt_col in ("datetime", "snapshot_time", "created_at", "updated_at"):
        if dt_col in colset:
            queries.append((f"{dt_col}_date", f"SELECT DISTINCT symbol FROM {table_name} WHERE DATE({dt_col}) = :target_ymd"))
            queries.append((f"{dt_col}_like", f"SELECT DISTINCT symbol FROM {table_name} WHERE CAST({dt_col} AS TEXT) LIKE :target_prefix"))

    return queries


def read_ranking_symbols_for_date(conn, target_date: dt.date) -> List[str]:
    table_candidates = ("ranking_snapshot_1min", "ranking_snapshot", "ranking_raw_1min", "ranking_raw")
    target_ymd = str(target_date)
    target_compact = target_date.strftime("%Y%m%d")
    target_prefix = f"{target_ymd}%"

    for table_name in table_candidates:
        try:
            columns = fetch_table_columns(conn, table_name)
            if not columns:
                continue

            params = {"target_ymd": target_ymd, "target_compact": target_compact, "target_prefix": target_prefix}
            for q_name, sql in build_ranking_symbol_queries(table_name, columns):
                try:
                    df = pd.read_sql(text(sql), conn, params=params)
                    if not df.empty and "symbol" in df.columns:
                        out = filter_symbol_list(df["symbol"].astype(str).tolist())
                        if out:
                            logger.info("✅ ranking symbol read success table=%s query=%s count=%d", table_name, q_name, len(out))
                            return out
                except Exception:
                    logger.debug("ranking symbol read failed table=%s query=%s", table_name, q_name, exc_info=True)
        except Exception:
            logger.exception("ranking symbol read failed table=%s", table_name)

    return []


def read_ranking_symbols_from_db_path(db_path: str, target_date: dt.date) -> List[str]:
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        with sqlite3.connect(db_path, timeout=15) as conn:
            return read_ranking_symbols_for_date(conn, target_date)
    except Exception:
        logger.exception("ranking symbol read from db_path failed path=%s", db_path)
        return []


def load_union_ranking_symbols() -> List[str]:
    symbols: Set[str] = set()
    today = dt.date.today()
    prev_bd = get_previous_business_day(today)

    try:
        prev_db = resolve_ranking_db_path_for_date(prev_bd)
        if prev_db:
            symbols.update(read_ranking_symbols_from_db_path(prev_db, prev_bd))
    except Exception:
        logger.exception("previous business day ranking extraction failed")

    if is_today_business_day():
        try:
            today_db = resolve_ranking_db_path_for_date(today)
            if today_db:
                symbols.update(read_ranking_symbols_from_db_path(today_db, today))
        except Exception:
            logger.exception("today ranking extraction failed")

    if not symbols:
        try:
            engine = get_ranking_engine()
            if engine is not None:
                with engine.connect() as conn:
                    symbols.update(read_ranking_symbols_for_date(conn, prev_bd))
                    if is_today_business_day():
                        symbols.update(read_ranking_symbols_for_date(conn, today))
        except Exception:
            logger.exception("ranking union fallback engine extraction failed")

    out = filter_symbol_list(sorted(symbols))
    logger.info("🎯 ranking strict union symbols=%d", len(out))
    return out


def multi_day_coverage_ok(tf: int, df: pd.DataFrame, expected_symbols: int) -> bool:
    try:
        got = safe_symbol_nunique(df)
        ratio = got / max(1, expected_symbols)
        min_abs = _MIN_MULTI_DAY_SYMBOL_COVERAGE_ABS.get(int(tf), 50)
        min_ratio = _MIN_MULTI_DAY_SYMBOL_COVERAGE_RATIO.get(int(tf), 0.10)
        ok = (got >= min_abs) and (ratio >= min_ratio)

        logger.info(
            "[BOOT CHECK][%smin/multi_day_coverage] got=%d expected=%d ratio=%.3f min_abs=%d min_ratio=%.3f ok=%s",
            tf, got, expected_symbols, ratio, min_abs, min_ratio, ok,
        )
        return ok
    except Exception:
        logger.debug("multi-day coverage check failed tf=%s", tf, exc_info=True)
        return False


def preload_multi_day_ranking_based() -> None:
    try:
        symbols = load_union_ranking_symbols()
    except Exception:
        logger.exception("ranking union extraction failed")
        symbols = []

    if not symbols:
        logger.warning("⚠ ranking union empty → fallback recent universe")
        try:
            fallback_symbols: Set[str] = set()
            for tf in DEFAULT_INTERVALS:
                df = global_data.latest_summary_by_interval.get(tf)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    s = safe_symbol_series(df)
                    if s is not None:
                        fallback_symbols.update(s.astype(str).tolist())
            symbols = filter_symbol_list(sorted(fallback_symbols))
        except Exception:
            logger.exception("recent fallback symbol extraction failed")
            symbols = []

    if not symbols:
        logger.warning("⚠ no symbols available → multi-day preload skipped")
        return

    logger.info("🚀 multi-day ranking-based preload symbols=%d", len(symbols))

    for tf in DEFAULT_INTERVALS:
        try:
            df = load_multi_day_summary(interval=tf, symbols=symbols)
            df = drop_rows_outside_allowed_dates(
                df,
                label="multi_day_raw",
                include_previous_business_day=True,
                interval=int(tf),
            )

            if isinstance(df, pd.DataFrame) and not df.empty:
                log_boot_df("multi_day_raw", tf, df)
                df = normalize_summary_frame(df, tf=tf)
                df = backfill_symbolname(df)
                log_boot_df("multi_day_normalized", tf, df)
                df = apply_market_filter_df(df)
                log_boot_df("multi_day_filtered", tf, df)

                if int(tf) in (3, 5) and not multi_day_coverage_ok(tf, df, len(symbols)):
                    logger.warning(
                        "⚠ multi-day preload sparse -> skip storing tf=%s rows=%d symbols=%d expected=%d",
                        tf, len(df), safe_symbol_nunique(df), len(symbols),
                    )
                    continue

                if not df.empty:
                    processed = early_process_preload_df(df, tf, "multi_day")
                    safe_df = processed.copy(deep=True) if isinstance(processed, pd.DataFrame) and not processed.empty else ensure_summary_display_columns(normalize_summary_frame(df, tf=tf)).copy(deep=True)

                    try:
                        if hasattr(global_data, "set_multi_summary"):
                            global_data.set_multi_summary(tf, safe_df.copy(deep=True))
                        else:
                            setattr(global_data, f"multi_summary_{tf}", safe_df.copy(deep=True))
                    except Exception:
                        logger.debug("set_multi_summary failed tf=%s", tf, exc_info=True)

                    persist_summary_df_to_db(safe_df, tf, stage="multi_day_preload")
                    logger.info("📚 multi-day %smin rows=%d symbols=%d", tf, len(safe_df), safe_symbol_nunique(safe_df))
                else:
                    logger.warning("⚠ multi-day preload filtered empty %smin", tf)
            else:
                logger.warning("⚠ multi-day preload empty %smin", tf)

        except Exception:
            logger.exception("multi-day preload failed tf=%s", tf)


__all__ = [
    "early_process_preload_df",
    "get_recent_preload_df",
    "preload_recent_summaries",
    "run_initial_summary_preload_if_available",
    "resolve_ranking_db_path_for_date",
    "fetch_table_columns",
    "build_ranking_symbol_queries",
    "read_ranking_symbols_for_date",
    "read_ranking_symbols_from_db_path",
    "load_union_ranking_symbols",
    "multi_day_coverage_ok",
    "preload_multi_day_ranking_based",
]