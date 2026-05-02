# ============================================================
# File   : trading/summary/engine/incremental/history.py
# Version: Ver1.1-INCREMENTAL-HISTORY
#          -PREV-BUSINESS-DAY-WARMUP
#          -MERGED-SUMMARY-SAFE-STORE
#          -DB-COMPAT
# ------------------------------------------------------------
# 目的:
#   - incremental pipeline 用の履歴結合
#   - 前営業日を含む summary 履歴を安全に取得
#   - 当日再計算DFと履歴DFを symbol+datetime で結合
#   - merged summary を global_data へ安全保存
#
# 想定インターフェース:
#   - merge_with_history(df, interval) -> pd.DataFrame
#   - store_merged_summary_safe(interval, df) -> None
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# optional imports
# ============================================================

try:
    from core.global_context.context import global_data  # type: ignore
except Exception:
    try:
        from global_state import global_data  # type: ignore
    except Exception:
        global_data = None

try:
    from utils.market_calendar import get_previous_business_day  # type: ignore
except Exception:
    def get_previous_business_day(d: dt.date) -> dt.date:
        x = d - dt.timedelta(days=1)
        while x.weekday() >= 5:
            x -= dt.timedelta(days=1)
        return x

try:
    import sqlite3
except Exception:
    sqlite3 = None


# ============================================================
# config
# ============================================================

_INTERVAL_TABLE_MAP = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
    10: "stock_summary_10min",
    15: "stock_summary_15min",
    30: "stock_summary_30min",
    60: "stock_summary_60min",
}

_DEFAULT_SUMMARY_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary"

_BASE_COLS = [
    "symbol",
    "symbolname",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "vwap",
]


# ============================================================
# basic helpers
# ============================================================

def _ensure_df(df: pd.DataFrame | None) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df.copy()
    return pd.DataFrame()


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df)
    if out.empty:
        return out

    for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time"):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce")
            try:
                out[c] = out[c].dt.tz_localize(None)
            except Exception:
                pass

    if "datetime" not in out.columns:
        for c in ("dt", "timestamp", "end_time", "snapshot_time"):
            if c in out.columns:
                out["datetime"] = pd.to_datetime(out[c], errors="coerce")
                try:
                    out["datetime"] = out["datetime"].dt.tz_localize(None)
                except Exception:
                    pass
                break

    return out


def _ensure_numeric(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df)
    for c in ("open", "high", "low", "close", "volume", "turnover", "vwap"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _sort_symbol_dt(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df)
    if out.empty:
        return out

    out = _ensure_datetime(out)

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str)

    if "symbol" in out.columns and "datetime" in out.columns:
        out = out.sort_values(["symbol", "datetime"], kind="stable")

    return out.reset_index(drop=True)


def _safe_symbols(df: pd.DataFrame) -> int:
    try:
        if isinstance(df, pd.DataFrame) and "symbol" in df.columns:
            return int(df["symbol"].astype(str).nunique())
    except Exception:
        pass
    return 0


def _safe_latest_dt(df: pd.DataFrame):
    try:
        if isinstance(df, pd.DataFrame) and "datetime" in df.columns:
            s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
            if not s.empty:
                x = s.max()
                try:
                    x = x.tz_localize(None)
                except Exception:
                    pass
                return x
    except Exception:
        pass
    return None


def _log_df_state(tag: str, df: pd.DataFrame) -> None:
    try:
        if df is None or df.empty:
            logger.warning("[incremental.history][%s] empty", tag)
            return

        logger.info(
            "[incremental.history][%s] rows=%s symbols=%s latest_dt=%s cols=%s",
            tag,
            len(df),
            _safe_symbols(df),
            _safe_latest_dt(df),
            list(df.columns),
        )
    except Exception:
        logger.exception("[incremental.history] _log_df_state failed tag=%s", tag)


# ============================================================
# path / db helpers
# ============================================================

def _resolve_summary_root() -> str:
    # 1) global_data
    try:
        if global_data is not None:
            for attr in ("summary_root", "summary_db_root", "summary_dir"):
                v = getattr(global_data, attr, None)
                if isinstance(v, str) and v.strip():
                    return v
    except Exception:
        pass

    # 2) env-like fallback not used here
    return _DEFAULT_SUMMARY_ROOT


def _summary_db_path_for_date(target_date: dt.date) -> str:
    root = _resolve_summary_root()
    return str(Path(root) / f"summary{target_date.strftime('%Y%m%d')}.db")


def _table_name(interval: int) -> str:
    return _INTERVAL_TABLE_MAP.get(int(interval), f"stock_summary_{int(interval)}min")


def _read_sqlite_table(path: str, sql: str, params: Optional[tuple] = None) -> pd.DataFrame:
    if sqlite3 is None:
        logger.warning("[incremental.history] sqlite3 unavailable path=%s", path)
        return pd.DataFrame()

    if not path or not Path(path).exists():
        logger.warning("[incremental.history] summary db not found path=%s", path)
        return pd.DataFrame()

    try:
        with sqlite3.connect(path, timeout=30) as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
            except Exception:
                pass
            try:
                conn.execute("PRAGMA busy_timeout=30000;")
            except Exception:
                pass
            return pd.read_sql_query(sql, conn, params=params or ())
    except Exception:
        logger.exception("[incremental.history] read sqlite failed path=%s", path)
        return pd.DataFrame()


# ============================================================
# global_data helpers
# ============================================================

def _get_global_merged_summary(interval: int) -> pd.DataFrame:
    if global_data is None:
        return pd.DataFrame()

    # 1) getter
    try:
        getter = getattr(global_data, "get_merged_summary", None)
        if callable(getter):
            df = getter(int(interval))
            if isinstance(df, pd.DataFrame):
                return df.copy()
    except Exception:
        logger.exception("[incremental.history] global get_merged_summary failed interval=%s", interval)

    # 2) common attrs
    for attr in (
        f"summary_{int(interval)}m_df",
        f"merged_summary_{int(interval)}m",
        f"summary_df_{int(interval)}m",
    ):
        try:
            df = getattr(global_data, attr, None)
            if isinstance(df, pd.DataFrame):
                return df.copy()
        except Exception:
            continue

    return pd.DataFrame()


def store_merged_summary_safe(interval: int, df: pd.DataFrame) -> None:
    out = _sort_symbol_dt(_ensure_numeric(_ensure_datetime(_ensure_df(df))))
    if out.empty:
        logger.warning(
            "[incremental.history] store_merged_summary_safe skipped interval=%s reason=empty",
            interval,
        )
        return

    if global_data is None:
        logger.warning(
            "[incremental.history] store_merged_summary_safe skipped interval=%s reason=no_global_data",
            interval,
        )
        return

    try:
        setter = getattr(global_data, "set_merged_summary", None)
        if callable(setter):
            setter(int(interval), out)
            logger.info(
                "[incremental.history] stored via set_merged_summary interval=%s rows=%s symbols=%s latest_dt=%s",
                interval,
                len(out),
                _safe_symbols(out),
                _safe_latest_dt(out),
            )
            return
    except Exception:
        logger.exception(
            "[incremental.history] set_merged_summary failed interval=%s",
            interval,
        )

    # fallback attr
    for attr in (
        f"summary_{int(interval)}m_df",
        f"merged_summary_{int(interval)}m",
        f"summary_df_{int(interval)}m",
    ):
        try:
            setattr(global_data, attr, out)
            logger.info(
                "[incremental.history] stored via attr=%s interval=%s rows=%s symbols=%s latest_dt=%s",
                attr,
                interval,
                len(out),
                _safe_symbols(out),
                _safe_latest_dt(out),
            )
            return
        except Exception:
            continue

    logger.warning(
        "[incremental.history] store_merged_summary_safe no writable target interval=%s",
        interval,
    )


# ============================================================
# history loaders
# ============================================================

def _prepare_history_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df)
    if out.empty:
        return out

    out = _ensure_datetime(out)
    out = _ensure_numeric(out)

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str)

    if "datetime" not in out.columns:
        logger.warning("[incremental.history] history df has no datetime")
        return pd.DataFrame()

    # base cols fill
    for c in _BASE_COLS:
        if c not in out.columns:
            out[c] = np.nan

    out = out.dropna(subset=["symbol", "datetime"]).copy()
    out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    out = _sort_symbol_dt(out)
    return out


def _load_from_global_cache(interval: int) -> pd.DataFrame:
    df = _get_global_merged_summary(interval)
    df = _prepare_history_df(df)
    _log_df_state(f"global-cache-{interval}m", df)
    return df


def _load_from_summary_db(interval: int, target_date: dt.date) -> pd.DataFrame:
    table = _table_name(interval)
    path = _summary_db_path_for_date(target_date)

    sql = f"""
        SELECT *
        FROM {table}
        ORDER BY datetime ASC
    """

    df = _read_sqlite_table(path, sql)
    df = _prepare_history_df(df)

    logger.info(
        "[incremental.history] loaded from summary db interval=%s date=%s path=%s rows=%s symbols=%s latest_dt=%s",
        interval,
        target_date,
        path,
        len(df),
        _safe_symbols(df),
        _safe_latest_dt(df),
    )
    return df


def _target_dates_from_df(df: pd.DataFrame) -> tuple[Optional[dt.date], Optional[dt.date]]:
    out = _ensure_datetime(_ensure_df(df))
    if out.empty or "datetime" not in out.columns:
        return None, None

    s = pd.to_datetime(out["datetime"], errors="coerce").dropna()
    if s.empty:
        return None, None

    current_date = s.max().date()
    prev_date = get_previous_business_day(current_date)
    return current_date, prev_date


def _trim_history_window(history_df: pd.DataFrame, current_df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    ウォームアップ用に前営業日後半 + 当日先頭から現在まで を残す。
    ここでは厳しすぎる切り詰めはせず、原則そのまま返す。
    """
    hist = _prepare_history_df(history_df)
    cur = _prepare_history_df(current_df)

    if hist.empty:
        return hist
    if cur.empty or "datetime" not in cur.columns:
        return hist

    cur_latest = _safe_latest_dt(cur)
    if cur_latest is None:
        return hist

    # 5分足や MACD を考慮して前営業日分は基本的に残す
    # ただし未来データだけ落とす
    hist = hist[pd.to_datetime(hist["datetime"], errors="coerce") <= cur_latest].copy()
    hist = _sort_symbol_dt(hist)

    logger.info(
        "[incremental.history] trim window interval=%s hist_rows=%s cur_rows=%s cur_latest=%s",
        interval,
        len(hist),
        len(cur),
        cur_latest,
    )
    return hist


# ============================================================
# public API
# ============================================================

def merge_with_history(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    当日再計算対象 df に前営業日を含む履歴を結合する。

    優先順:
      1. global_data の merged summary cache
      2. 前営業日の summary DB
      3. 当日 summary DB
    """
    current = _prepare_history_df(df)
    if current.empty:
        logger.warning(
            "[incremental.history] merge_with_history skipped interval=%s reason=current_empty",
            interval,
        )
        return pd.DataFrame()

    current_date, prev_date = _target_dates_from_df(current)
    logger.info(
        "[incremental.history] merge start interval=%s current_rows=%s symbols=%s current_date=%s prev_date=%s latest_dt=%s",
        interval,
        len(current),
        _safe_symbols(current),
        current_date,
        prev_date,
        _safe_latest_dt(current),
    )

    # 1) global cache
    history_parts: list[pd.DataFrame] = []
    cached = _load_from_global_cache(interval)
    if not cached.empty:
        history_parts.append(cached)

    # 2) previous business day DB
    if prev_date is not None:
        prev_df = _load_from_summary_db(interval, prev_date)
        if not prev_df.empty:
            history_parts.append(prev_df)

    # 3) current day DB
    if current_date is not None:
        today_df = _load_from_summary_db(interval, current_date)
        if not today_df.empty:
            history_parts.append(today_df)

    if history_parts:
        history = pd.concat(history_parts, ignore_index=True)
        history = _prepare_history_df(history)
        history = _trim_history_window(history, current, interval)
    else:
        history = pd.DataFrame()

    _log_df_state(f"history-before-merge-{interval}m", history)

    # merge
    merged_parts = []
    if not history.empty:
        merged_parts.append(history)
    merged_parts.append(current)

    merged = pd.concat(merged_parts, ignore_index=True)
    merged = _prepare_history_df(merged)

    # 最新計算結果を優先
    merged = merged.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    merged = _sort_symbol_dt(merged)

    logger.info(
        "[incremental.history] merge done interval=%s out_rows=%s symbols=%s latest_dt=%s",
        interval,
        len(merged),
        _safe_symbols(merged),
        _safe_latest_dt(merged),
    )
    return merged