# ============================================================
# File   : trading/summary/persistence/summary_loader.py
# Version: Ver3.5-PRODUCTION-SUMMARY-LOADER-MULTI-DAY-DATETIME-RECOVERY
#          -NONDESTRUCTIVE-NORMALIZE-INVALID-OHLC-DROP-HARDENED-FINAL
#          -BUSINESS-DAY-GUARD-CLOSED-DAY-SAFE
#          -RECENT-1MIN-POSTGUARD-BARS-FIX
# ------------------------------------------------------------
# ✔ Ver3.4 機能完全保持（削除ゼロ）
# ✔ ORM / text 両対応
# ✔ parse_dates=False 強制思想維持
# ✔ datetime完全防御
# ✔ normalize前後ガード
# ✔ open_price/high_price/low_price/close_price 正式対応
# ✔ open/high/low/close 旧列にも後方互換対応
# ✔ CurrentPrice/current_price/ClosePrice/LastPrice 吸収追加
# ✔ TradingVolume/trading_volume/Volume 吸収追加
# ✔ テーブル実在列を見て SELECT を自動切替
# ✔ 1min / 3min / 5min 共通安全化
# ✔ load_multi_day_summary を実テーブル直読へ修正維持
# ✔ interval別 recent loader を実テーブル共通化維持
# ✔ symbolごとの履歴本数確保維持
# ✔ latest only 問題解消維持
# ✔ datetime fallback recovery (end_time/start_time/time) 維持
# ✔ datetime availability log 維持
# ✔ normalize_all の過補完を回避維持
# ✔ loader後は non-destructive normalize のみ維持
# ✔ invalid OHLC 可視化ログ維持
# ✔ invalid OHLC row drop at loader stage 維持
# ✔ 価格0/負値 drop 維持
# ✔ bootstrap混入前に不正OHLC除外 維持
# ✔ 1分足は close 生存最低条件 + OHLC最小補完
# ✔ time_range から datetime 復元
# ✔ 休場日は前営業日、営業日は today/prev_bd のみ許可
# ✔ recent loader で business-day guard 適用
# ✔ NEW: 1min recent loader も 3min/5min と同じく
#        post_process/date guard 後に bars 制限する
# ✔ production safe（完全版）
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text

from database.models import StockSummary1Min
from database.session import Session_summary
from utils.normalize import (
    normalize_datetime,
    normalize_numeric,
    normalize_symbol,
)

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

_TABLE_MAP = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

_DEFAULT_RECENT_BARS = {
    1: 300,
    3: 200,
    5: 200,
}


# ============================================================
# business-day helpers
# ============================================================

def _today() -> dt.date:
    return dt.datetime.now().date()


def _get_previous_business_day(base_date: dt.date) -> dt.date:
    try:
        from utils.business_day_utils import get_previous_business_day  # type: ignore
        return get_previous_business_day(base_date)
    except Exception:
        d = base_date - dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d


def _allowed_loader_dates() -> set[dt.date]:
    today = _today()
    prev_bd = _get_previous_business_day(today)

    try:
        from utils.business_day_utils import is_today_business_day  # type: ignore
        business_day = bool(is_today_business_day())
    except Exception:
        business_day = today.weekday() < 5

    if business_day:
        return {prev_bd, today}

    return {prev_bd}


# ============================================================
# COLUMN RESOLUTION HELPERS
# ============================================================

def _fetch_table_columns(session, table: str) -> List[str]:
    try:
        rows = session.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return [str(r[1]) for r in rows if len(r) > 1 and r[1] is not None]
    except Exception:
        logger.exception("[SUMMARY LOADER] failed to fetch table columns: %s", table)
        return []


def _resolve_ohlc_column_map(columns: List[str]) -> Dict[str, str]:
    colset = {str(c) for c in columns}
    mapping: Dict[str, str] = {}

    if "open_price" in colset:
        mapping["open"] = "open_price"
    elif "open" in colset:
        mapping["open"] = "open"

    if "high_price" in colset:
        mapping["high"] = "high_price"
    elif "high" in colset:
        mapping["high"] = "high"

    if "low_price" in colset:
        mapping["low"] = "low_price"
    elif "low" in colset:
        mapping["low"] = "low"

    if "close_price" in colset:
        mapping["close"] = "close_price"
    elif "close" in colset:
        mapping["close"] = "close"

    return mapping


def _build_select_parts(columns: List[str]) -> List[str]:
    colset = {str(c) for c in columns}
    select_parts: List[str] = [
        "symbol" if "symbol" in colset else "NULL AS symbol",
        "symbolname" if "symbolname" in colset else "NULL AS symbolname",
        "CAST(datetime AS TEXT) AS datetime" if "datetime" in colset else "NULL AS datetime",
        "CAST(date AS TEXT) AS date" if "date" in colset else "NULL AS date",
        "CAST(time AS TEXT) AS time" if "time" in colset else "NULL AS time",
        "CAST(start_time AS TEXT) AS start_time" if "start_time" in colset else "NULL AS start_time",
        "CAST(end_time AS TEXT) AS end_time" if "end_time" in colset else "NULL AS end_time",
        "CAST(time_range AS TEXT) AS time_range" if "time_range" in colset else "NULL AS time_range",
        "interval" if "interval" in colset else "NULL AS interval",
        "interval_name" if "interval_name" in colset else "NULL AS interval_name",
    ]

    ohlc_map = _resolve_ohlc_column_map(columns)

    select_parts.append(f"{ohlc_map['open']} AS open" if "open" in ohlc_map else "NULL AS open")
    select_parts.append(f"{ohlc_map['high']} AS high" if "high" in ohlc_map else "NULL AS high")
    select_parts.append(f"{ohlc_map['low']} AS low" if "low" in ohlc_map else "NULL AS low")
    select_parts.append(f"{ohlc_map['close']} AS close" if "close" in ohlc_map else "NULL AS close")

    if "volume" in colset:
        select_parts.append("volume")
    elif "trading_volume" in colset:
        select_parts.append("trading_volume AS volume")
    else:
        select_parts.append("NULL AS volume")

    optional_cols = [
        "score", "score_buy", "score_sell", "score_total", "score_slope", "score_mtf",
        "slope", "mtf", "ma5", "ma25", "ma75", "rsi", "macd", "signal", "hist",
        "atr", "slope_atr_scaled", "buy_score", "sell_score", "combined_score",
        "final_score", "display_score", "ai_score", "name", "score_reason", "cluster",
        "price", "current_price", "CurrentPrice", "last_price", "LastPrice",
        "trading_volume", "volume_speed", "source",
    ]
    for col in optional_cols:
        if col in colset:
            select_parts.append(col)

    return select_parts


def _build_latest_select_sql(table: str, columns: List[str], limit_rows: int = 5000) -> str:
    colset = {str(c) for c in columns}
    select_parts = _build_select_parts(columns)

    where_clause = "datetime IS NOT NULL" if "datetime" in colset else "1=1"
    order_clause = "datetime DESC" if "datetime" in colset else "rowid DESC"

    sql = f"""
        SELECT
            {", ".join(select_parts)}
        FROM {table}
        WHERE {where_clause}
        ORDER BY {order_clause}
        LIMIT {int(limit_rows)}
    """
    return sql


def _build_history_select_sql(
    table: str,
    columns: List[str],
    start_dt: Optional[dt.datetime] = None,
    end_dt: Optional[dt.datetime] = None,
    symbols: Optional[List[str]] = None,
    limit_rows: Optional[int] = None,
) -> Tuple[str, Dict[str, object]]:
    colset = {str(c) for c in columns}
    select_parts = _build_select_parts(columns)

    where_parts: List[str] = []
    params: Dict[str, object] = {}

    if "datetime" in colset:
        where_parts.append("datetime IS NOT NULL")

    if start_dt is not None and "datetime" in colset:
        where_parts.append("datetime >= :start_dt")
        params["start_dt"] = str(start_dt)

    if end_dt is not None and "datetime" in colset:
        where_parts.append("datetime <= :end_dt")
        params["end_dt"] = str(end_dt)

    if symbols and "symbol" in colset:
        symbol_params: List[str] = []
        for i, sym in enumerate(symbols):
            key = f"sym_{i}"
            symbol_params.append(f":{key}")
            params[key] = str(sym)
        if symbol_params:
            where_parts.append(f"symbol IN ({', '.join(symbol_params)})")

    where_clause = " AND ".join(where_parts) if where_parts else "1=1"
    order_clause = "symbol, datetime" if "datetime" in colset and "symbol" in colset else "rowid"

    sql = f"""
        SELECT
            {", ".join(select_parts)}
        FROM {table}
        WHERE {where_clause}
        ORDER BY {order_clause}
    """

    if limit_rows:
        sql += f"\nLIMIT {int(limit_rows)}"

    return sql, params


# ============================================================
# logical OHLC / alias helpers
# ============================================================

def _ensure_logical_ohlc_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    logical_map = {
        "open": ["open", "open_price", "Open", "OpenPrice", "opening_price", "OpeningPrice"],
        "high": ["high", "high_price", "High", "HighPrice"],
        "low": ["low", "low_price", "Low", "LowPrice"],
        "close": [
            "close", "close_price", "Close", "ClosePrice",
            "price", "Price",
            "current_price", "CurrentPrice",
            "last_price", "LastPrice",
        ],
        "volume": ["volume", "Volume", "trading_volume", "TradingVolume", "volume_total"],
    }

    for logical, candidates in logical_map.items():
        if logical not in df.columns:
            for c in candidates:
                if c in df.columns:
                    df[logical] = df[c]
                    break
        if logical not in df.columns:
            df[logical] = pd.NA

    alias_pairs = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "price": "close",
        "current_price": "close",
        "CurrentPrice": "close",
        "last_price": "close",
        "LastPrice": "close",
        "trading_volume": "volume",
        "TradingVolume": "volume",
    }

    for alias, src in alias_pairs.items():
        if alias not in df.columns and src in df.columns:
            df[alias] = df[src]

    return df


def _sanitize_price_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    for c in [
        "open", "high", "low", "close",
        "open_price", "high_price", "low_price", "close_price",
        "price", "current_price", "CurrentPrice", "last_price", "LastPrice",
    ]:
        if c in out.columns:
            s = pd.to_numeric(out[c], errors="coerce").replace([np.inf, -np.inf], np.nan)
            s = s.mask(s <= 0, np.nan)
            out[c] = s

    if "volume" in out.columns:
        out["volume"] = (
            pd.to_numeric(out["volume"], errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

    if "trading_volume" in out.columns:
        out["trading_volume"] = (
            pd.to_numeric(out["trading_volume"], errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

    return out


def _log_invalid_ohlc(df: pd.DataFrame, stage: str) -> None:
    try:
        if df is None or df.empty:
            return

        needed = {"open", "high", "low", "close"}
        if not needed.issubset(df.columns):
            return

        o = pd.to_numeric(df["open"], errors="coerce")
        h = pd.to_numeric(df["high"], errors="coerce")
        l = pd.to_numeric(df["low"], errors="coerce")
        c = pd.to_numeric(df["close"], errors="coerce")

        invalid = (
            o.isna() | h.isna() | l.isna() | c.isna()
            | (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)
            | (h < l) | (h < o) | (h < c)
            | (l > o) | (l > c)
        )

        cnt = int(invalid.fillna(False).sum())
        if cnt > 0:
            sample_cols = [
                x for x in [
                    "symbol", "datetime", "end_time",
                    "open", "high", "low", "close",
                    "close_price", "price", "current_price", "CurrentPrice", "last_price",
                ]
                if x in df.columns
            ]
            logger.warning(
                "[SUMMARY LOADER] invalid OHLC detected stage=%s count=%d sample=\n%s",
                stage,
                cnt,
                df.loc[invalid, sample_cols].head(20).to_string(index=False),
            )
    except Exception:
        logger.exception("[SUMMARY LOADER] invalid OHLC log failed stage=%s", stage)


def _drop_invalid_ohlc_rows(df: pd.DataFrame, interval: int | None = None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()
    needed = {"open", "high", "low", "close"}
    if not needed.issubset(out.columns):
        return out

    o = pd.to_numeric(out["open"], errors="coerce")
    h = pd.to_numeric(out["high"], errors="coerce")
    l = pd.to_numeric(out["low"], errors="coerce")
    c = pd.to_numeric(out["close"], errors="coerce")

    if int(interval or 1) == 1:
        o = o.combine_first(c)
        h = h.combine_first(c)
        l = l.combine_first(c)
        valid = (
            c.notna()
            & (c > 0)
            & o.notna() & h.notna() & l.notna()
            & (o > 0) & (h > 0) & (l > 0)
            & (h >= l) & (h >= o) & (h >= c)
            & (l <= o) & (l <= c)
        )
    else:
        valid = (
            o.notna() & h.notna() & l.notna() & c.notna()
            & (o > 0) & (h > 0) & (l > 0) & (c > 0)
            & (h >= l) & (h >= o) & (h >= c)
            & (l <= o) & (l <= c)
        )

    before = len(out)
    bad = out.loc[~valid].copy()

    if not bad.empty:
        sample_cols = [
            x for x in [
                "symbol", "datetime", "end_time",
                "open", "high", "low", "close",
                "close_price", "price", "current_price", "CurrentPrice", "last_price",
            ]
            if x in bad.columns
        ]
        logger.warning(
            "[SUMMARY LOADER] invalid OHLC dropped interval=%s removed=%d sample=\n%s",
            interval,
            len(bad),
            bad[sample_cols].head(20).to_string(index=False),
        )

    out = out.loc[valid].copy()

    if before != len(out):
        logger.warning(
            "[SUMMARY LOADER] invalid OHLC drop rows=%d -> %d interval=%s",
            before,
            len(out),
            interval,
        )

    return out


# ============================================================
# datetime sanitize（最重要）
# ============================================================

def _repair_datetime_from_time_range(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    if "time_range" not in out.columns:
        return out

    tr = out["time_range"].astype(str).str.strip()

    if "datetime" not in out.columns:
        out["datetime"] = pd.NaT

    dt_existing = pd.to_datetime(out["datetime"], errors="coerce")
    need_dt = dt_existing.isna()

    parsed_full = pd.to_datetime(tr, errors="coerce")

    hhmm = tr.str.extract(r"^\s*(\d{2}:\d{2})(?:\s*-\s*(\d{2}:\d{2}))?\s*$")
    start_hhmm = hhmm[0]
    end_hhmm = hhmm[1].fillna(start_hhmm)

    if "date" in out.columns:
        date_s = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        parsed_from_date_end = pd.to_datetime(date_s.astype(str) + " " + end_hhmm.astype(str), errors="coerce")
    else:
        parsed_from_date_end = pd.Series(pd.NaT, index=out.index)

    repaired = parsed_full.combine_first(parsed_from_date_end)

    try:
        out.loc[need_dt, "datetime"] = repaired.loc[need_dt]
    except Exception:
        logger.debug("[SUMMARY LOADER] time_range datetime repair assign failed", exc_info=True)

    return out


def _sanitize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    def _to_dt_series(col_name: str) -> pd.Series:
        if col_name not in df.columns:
            return pd.Series(pd.NaT, index=df.index)
        s = df[col_name].replace([0, "0", "", "nan", "NaT", "None", None], pd.NA)
        s = pd.to_datetime(s, errors="coerce")
        try:
            if getattr(s.dt, "tz", None) is not None:
                try:
                    s = s.dt.tz_convert(None)
                except Exception:
                    s = s.dt.tz_localize(None)
            pass
        except Exception:
            pass
        return s

    raw_dt = _to_dt_series("datetime")
    raw_start = _to_dt_series("start_time")
    raw_end = _to_dt_series("end_time")
    raw_time = _to_dt_series("time")

    logger.info(
        "[SUMMARY LOADER] datetime availability before sanitize rows=%d dt=%d start=%d end=%d time=%d",
        len(df),
        int(raw_dt.notna().sum()),
        int(raw_start.notna().sum()),
        int(raw_end.notna().sum()),
        int(raw_time.notna().sum()),
    )

    base_dt = raw_dt.copy()
    base_dt = base_dt.where(base_dt.notna(), raw_end)
    base_dt = base_dt.where(base_dt.notna(), raw_start)

    if "time" in df.columns:
        try:
            time_text = df["time"].astype(str).str.strip()
            time_only_mask = time_text.str.match(r"^\d{1,2}:\d{2}(:\d{2})?$", na=False)

            if time_only_mask.any():
                if "date" in df.columns:
                    date_text = df["date"].astype(str).str.strip()
                    combo = pd.to_datetime(date_text + " " + time_text, errors="coerce")
                    base_dt = base_dt.where(base_dt.notna(), combo)
                else:
                    base_dt = base_dt.where(base_dt.notna(), raw_time)
            else:
                base_dt = base_dt.where(base_dt.notna(), raw_time)
        except Exception:
            logger.debug("[SUMMARY LOADER] time-based datetime recovery failed", exc_info=True)

    df["datetime"] = base_dt
    df = _repair_datetime_from_time_range(df)

    before = len(df)
    df = df.dropna(subset=["datetime"]).copy()
    after = len(df)

    if before != after:
        logger.warning("[SUMMARY LOADER] dropped invalid datetime rows: %s", before - after)

    return df


def _drop_rows_outside_allowed_dates(df: pd.DataFrame, interval: int, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()
    if "datetime" not in out.columns:
        return out

    dt_s = pd.to_datetime(out["datetime"], errors="coerce")
    allowed = _allowed_loader_dates()
    keep = dt_s.dt.date.isin(allowed)

    before = len(out)
    removed = int((~keep.fillna(False)).sum())
    if removed > 0:
        sample_cols = [c for c in ["symbol", "datetime", "date", "time", "start_time", "end_time", "time_range", "source"] if c in out.columns]
        logger.warning(
            "[SUMMARY LOADER] date guard removed interval=%s stage=%s removed=%d before=%d allowed=%s sample=\n%s",
            interval,
            stage,
            removed,
            before,
            sorted(str(x) for x in allowed),
            out.loc[~keep.fillna(False), sample_cols].head(20).to_string(index=False) if sample_cols else "(no sample cols)",
        )

    out = out.loc[keep.fillna(False)].copy().reset_index(drop=True)
    return out


# ============================================================
# post process（normalize含む）
# ============================================================

def _post_process(df: pd.DataFrame, interval: int | None = None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = _ensure_logical_ohlc_columns(df)
    df = _sanitize_datetime(df)
    if df.empty:
        return df

    try:
        df = normalize_symbol(df)
        df = normalize_datetime(df)
        df = normalize_numeric(df)
    except Exception:
        logger.exception("[SUMMARY LOADER] normalize failed")
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    df = _ensure_logical_ohlc_columns(df)
    df = _sanitize_price_columns(df)
    df = _sanitize_datetime(df)
    if df.empty:
        return df

    if int(interval or 1) == 1 and not df.empty:
        try:
            close_s = pd.to_numeric(df["close"], errors="coerce")
            for c in ("open", "high", "low"):
                if c in df.columns:
                    cur = pd.to_numeric(df[c], errors="coerce")
                    df[c] = cur.combine_first(close_s)
        except Exception:
            logger.debug("[SUMMARY LOADER] 1min OHLC backfill from close failed", exc_info=True)

    try:
        sort_cols = []
        if "symbol" in df.columns:
            sort_cols.append("symbol")
        if "datetime" in df.columns:
            sort_cols.append("datetime")
        if sort_cols:
            df = df.sort_values(sort_cols, kind="stable").reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY LOADER] sort failed", exc_info=True)

    _log_invalid_ohlc(df, "post_process_before_drop")
    df = _drop_invalid_ohlc_rows(df, interval=interval)
    _log_invalid_ohlc(df, "post_process_after_drop")
    df = _drop_rows_outside_allowed_dates(df, interval=int(interval or 1), stage="post_process")

    return df


# ============================================================
# safe read_sql（完全互換）
# ============================================================

def _safe_read_sql(query, session, params=None) -> pd.DataFrame:
    try:
        if hasattr(query, "statement"):
            stmt = query.statement
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        else:
            sql = query

        df = pd.read_sql(sql, session.bind, params=params)

        if df is None or df.empty:
            return pd.DataFrame()

        return df

    except Exception:
        logger.exception("[SUMMARY LOADER] read_sql failed")
        return pd.DataFrame()


# ============================================================
# table helper
# ============================================================

def _resolve_table_name(interval: int) -> str:
    return _TABLE_MAP.get(int(interval), f"stock_summary_{int(interval)}min")


def _limit_bars_per_symbol(df: pd.DataFrame, bars: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if not bars:
        return df

    if "symbol" not in df.columns or "datetime" not in df.columns:
        return df

    return (
        df.sort_values(["symbol", "datetime"], kind="stable")
          .groupby("symbol", group_keys=False)
          .tail(int(bars))
          .reset_index(drop=True)
    )


def _history_profile_log(df: pd.DataFrame, label: str) -> None:
    try:
        if df is None or df.empty or "symbol" not in df.columns:
            logger.info("[SUMMARY LOADER] %s profile skipped empty", label)
            return

        grp = df.groupby("symbol").size()
        logger.info(
            "[SUMMARY LOADER] %s rows=%d symbols=%d min=%d p50=%d p90=%d max=%d",
            label,
            len(df),
            int(grp.shape[0]),
            int(grp.min()) if len(grp) else 0,
            int(grp.quantile(0.5)) if len(grp) else 0,
            int(grp.quantile(0.9)) if len(grp) else 0,
            int(grp.max()) if len(grp) else 0,
        )
    except Exception:
        logger.debug("[SUMMARY LOADER] profile log failed: %s", label, exc_info=True)


# ============================================================
# load latest
# ============================================================

def load_latest_summary(interval: int) -> pd.DataFrame:
    try:
        table = _resolve_table_name(interval)

        with Session_summary() as session:
            columns = _fetch_table_columns(session, table)
            if not columns:
                logger.warning("[SUMMARY LOADER] table columns not found: %s", table)
                return pd.DataFrame()

            query = _build_latest_select_sql(table, columns)
            df = _safe_read_sql(query, session)

        if df is None or df.empty:
            return pd.DataFrame()

        df = _post_process(df, interval=interval)
        return df

    except Exception:
        logger.exception("[SUMMARY LOADER] latest load failed")
        return pd.DataFrame()


# ============================================================
# load history from table
# ============================================================

def _load_history_summary(
    interval: int,
    symbols=None,
    start_dt: Optional[dt.datetime] = None,
    end_dt: Optional[dt.datetime] = None,
    bars: Optional[int] = None,
    limit_rows: Optional[int] = None,
) -> pd.DataFrame:
    try:
        table = _resolve_table_name(interval)

        symbols_list = None
        if symbols:
            symbols_list = [str(x).strip() for x in symbols if str(x).strip()]

        logger.info(
            "[SUMMARY LOADER] history load start interval=%s table=%s requested_symbols=%d start_dt=%s end_dt=%s bars=%s limit_rows=%s",
            interval,
            table,
            len(symbols_list) if symbols_list else 0,
            start_dt,
            end_dt,
            bars,
            limit_rows,
        )

        with Session_summary() as session:
            columns = _fetch_table_columns(session, table)
            if not columns:
                logger.warning("[SUMMARY LOADER] table columns not found: %s", table)
                return pd.DataFrame()

            sql, params = _build_history_select_sql(
                table=table,
                columns=columns,
                start_dt=start_dt,
                end_dt=end_dt,
                symbols=symbols_list,
                limit_rows=limit_rows,
            )

            df = _safe_read_sql(sql, session, params=params)

        if df.empty:
            logger.warning(
                "[SUMMARY LOADER] empty raw result interval=%s table=%s requested_symbols=%d",
                interval,
                table,
                len(symbols_list) if symbols_list else 0,
            )
            return df

        raw_rows = len(df)
        raw_symbols = int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0

        logger.info(
            "[SUMMARY LOADER] raw result interval=%s table=%s rows=%d symbols=%d",
            interval,
            table,
            raw_rows,
            raw_symbols,
        )

        df = _post_process(df, interval=interval)

        if df.empty:
            logger.warning(
                "[SUMMARY LOADER] post_process empty interval=%s table=%s raw_rows=%d raw_symbols=%d",
                interval,
                table,
                raw_rows,
                raw_symbols,
            )
            return df

        post_rows = len(df)
        post_symbols = int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0

        try:
            dt_min = df["datetime"].min() if "datetime" in df.columns else None
            dt_max = df["datetime"].max() if "datetime" in df.columns else None
        except Exception:
            dt_min = None
            dt_max = None

        logger.info(
            "[SUMMARY LOADER] post result interval=%s table=%s rows=%d symbols=%d dt_min=%s dt_max=%s",
            interval,
            table,
            post_rows,
            post_symbols,
            dt_min,
            dt_max,
        )

        if symbols_list and "symbol" in df.columns:
            req = set(map(str, symbols_list))
            got = set(df["symbol"].astype(str).str.strip().tolist())
            miss = sorted(req - got)

            logger.info(
                "[SUMMARY LOADER] requested=%d got=%d missing=%d missing_sample=%s interval=%s table=%s",
                len(req),
                len(got),
                len(miss),
                miss[:30],
                interval,
                table,
            )

        if bars:
            before_rows = len(df)
            before_symbols = int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0

            df = _limit_bars_per_symbol(df, int(bars))

            after_rows = len(df)
            after_symbols = int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0

            logger.info(
                "[SUMMARY LOADER] bars limited interval=%s table=%s bars=%s rows=%d->%d symbols=%d->%d",
                interval,
                table,
                bars,
                before_rows,
                after_rows,
                before_symbols,
                after_symbols,
            )

        return df.reset_index(drop=True)

    except Exception:
        logger.exception("[SUMMARY LOADER] history load failed interval=%s", interval)
        return pd.DataFrame()


# ============================================================
# load range
# ============================================================

def load_summary_range(interval: int, start, end) -> pd.DataFrame:
    try:
        start_dt = pd.to_datetime(start, errors="coerce")
        end_dt = pd.to_datetime(end, errors="coerce")

        df = _load_history_summary(
            interval=interval,
            start_dt=start_dt if pd.notna(start_dt) else None,
            end_dt=end_dt if pd.notna(end_dt) else None,
        )
        return df

    except Exception:
        logger.exception("[SUMMARY LOADER] range load failed")
        return pd.DataFrame()


# ============================================================
# safe wrapper
# ============================================================

def safe_load_latest(interval: int) -> pd.DataFrame:
    df = load_latest_summary(interval)

    if df is None or df.empty:
        return pd.DataFrame()

    return df


# ============================================================
# core loader（最重要）
# ============================================================

def load_prev_1min_summary_all(**kwargs) -> pd.DataFrame:
    """
    1分足 recent loader。

    重要:
    - 休場日/当日混入があるため、先に LIMIT で最新だけ掴む旧方式は不可
    - 3min/5min と同じく _load_history_summary(interval=1) を通し、
      post_process/date guard 後に bars 制限する
    """
    symbols = kwargs.get("symbols")
    start_time = kwargs.get("start_time")
    end_time = kwargs.get("end_time")
    bars = kwargs.get("bars")
    max_trade_days = kwargs.get("max_trade_days")

    try:
        start_dt = pd.to_datetime(start_time, errors="coerce") if start_time is not None else None
        end_dt = pd.to_datetime(end_time, errors="coerce") if end_time is not None else None

        if pd.isna(start_dt):
            start_dt = None
        if pd.isna(end_dt):
            end_dt = None

        limit_rows = None
        if bars:
            multiplier = len(symbols) if symbols else 1
            limit_rows = max(int(bars) * int(multiplier) * 12, 10000)

        df = _load_history_summary(
            interval=1,
            symbols=symbols,
            start_dt=start_dt,
            end_dt=end_dt,
            bars=None,
            limit_rows=limit_rows,
        )

        if df.empty:
            logger.warning("[SUMMARY LOADER] load_prev_1min_summary_all empty after history load")
            return df

        if max_trade_days and "datetime" in df.columns:
            work = df.copy()
            work["date"] = pd.to_datetime(work["datetime"], errors="coerce").dt.date
            keep_dates = sorted(work["date"].dropna().unique())[-int(max_trade_days):]
            work = work[work["date"].isin(keep_dates)].drop(columns=["date"])
            df = work

        if {"symbol", "datetime"}.issubset(df.columns):
            df = df.sort_values(["symbol", "datetime"], kind="stable")

        if bars:
            df = (
                df.groupby("symbol", group_keys=False)
                .tail(int(bars))
                .reset_index(drop=True)
            )

        _history_profile_log(df, "recent_1min")
        return df.reset_index(drop=True)

    except Exception:
        logger.exception("[SUMMARY LOADER] load_prev_1min_summary_all failed")
        return pd.DataFrame()
# ============================================================
# compatibility（旧API）
# ============================================================

def load_recent_1min(symbols=None, bars=300, **kwargs):
    return load_prev_1min_summary_all(
        symbols=symbols,
        bars=bars,
        **kwargs
    )


def load_recent_3min(symbols=None, bars=200, **kwargs):
    try:
        end_dt = kwargs.get("end_time")
        start_dt = kwargs.get("start_time")

        df = _load_history_summary(
            interval=3,
            symbols=symbols,
            start_dt=start_dt,
            end_dt=end_dt,
            bars=bars,
        )
        _history_profile_log(df, "recent_3min")
        return df
    except Exception:
        logger.exception("[SUMMARY LOADER] load_recent_3min failed")
        return pd.DataFrame()


def load_recent_5min(symbols=None, bars=200, **kwargs):
    try:
        end_dt = kwargs.get("end_time")
        start_dt = kwargs.get("start_time")

        df = _load_history_summary(
            interval=5,
            symbols=symbols,
            start_dt=start_dt,
            end_dt=end_dt,
            bars=bars,
        )
        _history_profile_log(df, "recent_5min")
        return df
    except Exception:
        logger.exception("[SUMMARY LOADER] load_recent_5min failed")
        return pd.DataFrame()


def load_multi_day_summary(
    interval: int,
    symbols=None,
    start_time=None,
    end_time=None,
    bars=None,
    max_trade_days=None,
):
    """
    複数営業日分の summary をロードする共通ローダー。

    目的:
    - bootstrap 時の multi-day preload
    - 1min / 3min / 5min の既存DBから複数日分を安全に取得
    - date guard / bars 制限 / symbol絞り込みに対応

    注意:
    - .dt アクセサ使用前に datetime 正規化必須
    - load_summary() という未定義関数には依存しない
    - 既存の _load_history_summary() を土台にする
    """
    try:
        interval = int(interval)

        limit_rows = None
        if bars:
            try:
                multiplier = len(symbols) if symbols else 1
                limit_rows = max(int(bars) * int(multiplier) * 8, 5000)
            except Exception:
                limit_rows = max(int(bars) * 8, 5000)

        df = _load_history_summary(
            interval=interval,
            symbols=symbols,
            start_dt=start_time,
            end_dt=end_time,
            bars=None,              # 先に絞らず後段で制御
            limit_rows=limit_rows,
        )

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            logger.warning(
                "[SUMMARY LOADER] load_multi_day_summary empty interval=%s",
                interval,
            )
            return pd.DataFrame()

        df = df.copy()

        logger.info(
            "[SUMMARY LOADER] multi_day raw interval=%s rows=%s symbols=%s cols=%s",
            interval,
            len(df),
            df["symbol"].nunique() if "symbol" in df.columns else 0,
            list(df.columns),
        )

        # --------------------------------------------------
        # datetime 正規化
        # --------------------------------------------------
        if "datetime" not in df.columns:
            logger.error(
                "[SUMMARY LOADER] load_multi_day_summary missing datetime column interval=%s cols=%s",
                interval,
                list(df.columns),
            )
            return pd.DataFrame()

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        before_dt = len(df)
        df = df[df["datetime"].notna()].copy()
        after_dt = len(df)

        if before_dt != after_dt:
            logger.warning(
                "[SUMMARY LOADER] dropped invalid datetime rows interval=%s rows=%s->%s",
                interval,
                before_dt,
                after_dt,
            )

        if df.empty:
            logger.warning(
                "[SUMMARY LOADER] load_multi_day_summary empty after datetime normalize interval=%s",
                interval,
            )
            return pd.DataFrame()

        # timezone 混在対策
        try:
            if getattr(df["datetime"].dtype, "tz", None) is not None:
                df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            try:
                df["datetime"] = (
                    pd.to_datetime(df["datetime"], errors="coerce", utc=True)
                    .dt.tz_convert(None)
                )
                df = df[df["datetime"].notna()].copy()
            except Exception:
                logger.exception(
                    "[SUMMARY LOADER] timezone normalize failed interval=%s",
                    interval,
                )
                return pd.DataFrame()

        # symbol 正規化
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str).str.strip()

        # 並び順安定化
        sort_cols = [c for c in ["symbol", "datetime"] if c in df.columns]
        if sort_cols:
            df = df.sort_values(sort_cols).reset_index(drop=True)

        # --------------------------------------------------
        # 日付列作成
        # --------------------------------------------------
        df["date"] = df["datetime"].dt.date

        # --------------------------------------------------
        # max_trade_days 制限
        # --------------------------------------------------
        if max_trade_days:
            try:
                max_trade_days = int(max_trade_days)
                unique_dates = sorted([d for d in df["date"].dropna().unique()])
                if len(unique_dates) > max_trade_days:
                    keep_dates = set(unique_dates[-max_trade_days:])
                    before_rows = len(df)
                    before_symbols = df["symbol"].nunique() if "symbol" in df.columns else 0

                    df = df[df["date"].isin(keep_dates)].copy()

                    after_rows = len(df)
                    after_symbols = df["symbol"].nunique() if "symbol" in df.columns else 0

                    logger.info(
                        "[SUMMARY LOADER] date guard interval=%s max_trade_days=%s rows=%s->%s symbols=%s->%s keep_dates=%s",
                        interval,
                        max_trade_days,
                        before_rows,
                        after_rows,
                        before_symbols,
                        after_symbols,
                        [str(x) for x in sorted(keep_dates)],
                    )
            except Exception:
                logger.exception(
                    "[SUMMARY LOADER] max_trade_days guard failed interval=%s max_trade_days=%s",
                    interval,
                    max_trade_days,
                )

        # --------------------------------------------------
        # bars 制限
        # --------------------------------------------------
        if bars:
            try:
                bars = int(bars)
                before_rows = len(df)
                before_symbols = df["symbol"].nunique() if "symbol" in df.columns else 0

                if "symbol" in df.columns:
                    df = (
                        df.sort_values(["symbol", "datetime"])
                          .groupby("symbol", group_keys=False)
                          .tail(bars)
                          .reset_index(drop=True)
                    )
                else:
                    df = df.sort_values("datetime").tail(bars).reset_index(drop=True)

                after_rows = len(df)
                after_symbols = df["symbol"].nunique() if "symbol" in df.columns else 0

                logger.info(
                    "[SUMMARY LOADER] bars limited interval=%s bars=%s rows=%s->%s symbols=%s->%s",
                    interval,
                    bars,
                    before_rows,
                    after_rows,
                    before_symbols,
                    after_symbols,
                )
            except Exception:
                logger.exception(
                    "[SUMMARY LOADER] bars limit failed interval=%s bars=%s",
                    interval,
                    bars,
                )

        logger.info(
            "[SUMMARY LOADER] multi_day result interval=%s rows=%s symbols=%s dt_min=%s dt_max=%s",
            interval,
            len(df),
            df["symbol"].nunique() if "symbol" in df.columns else 0,
            df["datetime"].min() if not df.empty else None,
            df["datetime"].max() if not df.empty else None,
        )

        return df.reset_index(drop=True)

    except Exception:
        logger.exception("[SUMMARY LOADER] load_multi_day_summary failed")
        return pd.DataFrame()


# ============================================================
# public API
# ============================================================

__all__ = [
    "load_latest_summary",
    "load_summary_range",
    "safe_load_latest",
    "load_prev_1min_summary_all",
    "load_recent_1min",
    "load_recent_3min",
    "load_recent_5min",
    "load_multi_day_summary",
]