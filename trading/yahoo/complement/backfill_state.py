from __future__ import annotations
import datetime as dt, logging, time
from typing import Iterable
import pandas as pd
from .target_resolver import normalize_symbols, extract_success_symbols_from_df, build_rows_by_symbol, build_last_bar_by_symbol
from .logging_utils import log_step
logger = logging.getLogger(__name__)
try:
    from trading.ranking.runtime_symbols import mark_yahoo_backfilled, mark_yahoo_backfill_failed
    _HAS_RANKING_CACHE = True
except Exception:
    _HAS_RANKING_CACHE = False
    def mark_yahoo_backfilled(symbols: Iterable[object], **kwargs): return None
    def mark_yahoo_backfill_failed(symbols: Iterable[object], **kwargs): return None
try:
    from trading.yahoo.storage.yahoo_backfill_status import ensure_yahoo_backfill_status_db, mark_backfill_success, mark_backfill_failed, restore_backfilled_symbols_to_runtime, get_yahoo_status_db_path
    _HAS_BACKFILL_DB = True
except Exception:
    _HAS_BACKFILL_DB = False
    def ensure_yahoo_backfill_status_db(*args, **kwargs): return ""
    def mark_backfill_success(*args, **kwargs): return 0
    def mark_backfill_failed(*args, **kwargs): return 0
    def restore_backfilled_symbols_to_runtime(*args, **kwargs): return 0
    def get_yahoo_status_db_path(*args, **kwargs): return ""

def restore_db_backfilled_state(target_date: dt.date) -> None:
    ts = time.time()
    if not _HAS_BACKFILL_DB:
        logger.info("[YAHOO COMPLEMENT] backfill db unavailable -> skip restore"); return
    try:
        db_path = ensure_yahoo_backfill_status_db(trade_date=target_date)
        restored = restore_backfilled_symbols_to_runtime(trade_date=target_date)
        logger.info("[YAHOO COMPLEMENT] restored db backfilled symbols=%s trade_date=%s db=%s", restored, target_date, db_path)
        log_step("restore_db_backfilled_done", ts, restored=restored)
    except Exception: logger.exception("[YAHOO COMPLEMENT] restore db backfilled state failed")

def mark_download_result(requested_symbols: Iterable[object], normalized_df: pd.DataFrame, *, label: str, target_date: dt.date) -> None:
    if not _HAS_RANKING_CACHE: return
    requested = normalize_symbols(requested_symbols); succeeded = extract_success_symbols_from_df(normalized_df)
    if not requested: return
    failed = requested - succeeded
    try:
        if succeeded: mark_yahoo_backfilled(succeeded, target_date=target_date)
        if failed: mark_yahoo_backfill_failed(failed, target_date=target_date)
        if _HAS_BACKFILL_DB:
            if succeeded: mark_backfill_success(succeeded, trade_date=target_date, rows_by_symbol=build_rows_by_symbol(normalized_df), last_bar_by_symbol=build_last_bar_by_symbol(normalized_df))
            if failed: mark_backfill_failed(failed, trade_date=target_date, error=f"{label}:download_or_normalize_failed")
        logger.info("[YAHOO COMPLEMENT] %s result requested=%d succeeded=%d failed=%d db=%s", label, len(requested), len(succeeded), len(failed), get_yahoo_status_db_path(trade_date=target_date) if _HAS_BACKFILL_DB else "")
    except Exception: logger.exception("[YAHOO COMPLEMENT] %s cache mark failed", label)
__all__ = ["restore_db_backfilled_state", "mark_download_result"]
