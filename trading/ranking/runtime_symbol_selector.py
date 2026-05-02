# ============================================================
# File   : trading/ranking/runtime_symbol_selector.py
# Version: Ver32.0-PRODUCTION-COMPLETE-RUNTIME-SYMBOL-SELECTOR-FINAL
# ------------------------------------------------------------
# ✔ 前営業日 09:00〜15:30
# ✔ 当日 09:00〜現在
# ✔ 土日祝対応
# ✔ DISTINCT symbol
# ✔ SQLite高速化設計
# ✔ 再起動安全
# ✔ 例外完全吸収
# ✔ エンジンNone防御
# ✔ 型正規化
# ✔ ログ強化
# ✔ global_data 連携
# ✔ global_state / core.global_context 互換
# ✔ JPX市場（プライム/スタンダード/グロース）のみ
# ✔ ETF/ETN/REIT/指数連動/レバ系除外
# ✔ 貸借銘柄抽出対応（売り候補用途）
# ✔ summary / ranking / global_data 統合
# ✔ symbol_flags 判定強化
# ✔ margin 真偽表現の吸収強化
# ✔ 本番向け defensive coding
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

try:
    from utils.business_day_utils import (
        get_previous_business_day,
        is_today_business_day,
    )
except Exception:  # pragma: no cover
    def is_today_business_day(day: Optional[dt.date] = None) -> bool:
        day = day or dt.date.today()
        return day.weekday() < 5

    def get_previous_business_day(day: Optional[dt.date] = None) -> dt.date:
        day = day or dt.date.today()
        cur = day - dt.timedelta(days=1)
        while cur.weekday() >= 5:
            cur -= dt.timedelta(days=1)
        return cur

try:
    from config.paths import get_path
except Exception:  # pragma: no cover
    def get_path(key: str, default=None):
        return default

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:  # pragma: no cover
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()

logger = logging.getLogger(__name__)

MARKET_OPEN = dt.time(9, 0)
MARKET_CLOSE = dt.time(15, 30)

ALLOWED_MARKET_TYPES = {"プライム", "スタンダード", "グロース"}

EXCLUDE_NAME_KEYWORDS = (
    "ETF",
    "ETN",
    "REIT",
    "指数",
    "インデックス",
    "連動",
    "レバ",
    "ダブル",
    "ベア",
    "インバース",
    "ブル",
    "日経平均",
    "ＴＯＰＩＸ",
    "TOPIX",
)

MARGIN_TRUE_VALUES = {
    "1", "true", "yes", "y", "貸借", "対象", "margin", "eligible",
    "ok", "enabled", "available", "貸借銘柄", "売建可", "売り可",
}

DEFAULT_FLAG_DB_CANDIDATES = [
    r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db",
    r"Y:\AutoStockBuyAndSell\Basic\symbol_flags.db",
    r"Y:\Basic\symbol_flags.db",
]


@dataclass(frozen=True)
class RuntimeWindow:
    date_from: str
    time_from: str
    date_to: str
    time_to: str

    def as_tuple(self) -> Tuple[str, str, str, str]:
        return (self.date_from, self.time_from, self.date_to, self.time_to)


# ============================================================
# 基本ユーティリティ
# ============================================================

def _safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in {"nan", "none", "nat"}:
            return ""
        return s
    except Exception:
        return ""


def _normalize_symbol(v: Any) -> str:
    s = _safe_str(v)
    if not s:
        return ""

    if "." in s:
        s = s.split(".", 1)[0].strip()

    return s


def _dedupe_keep_order(items: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()

    for x in items:
        s = _normalize_symbol(x)
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def _safe_bool_from_any(v: Any) -> bool:
    try:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and not pd.isna(v):
            return float(v) != 0.0
        s = _safe_str(v).lower()
        return s in MARGIN_TRUE_VALUES
    except Exception:
        return False


def _is_business_day(day: Optional[dt.date] = None) -> bool:
    try:
        return bool(is_today_business_day(day))
    except TypeError:
        try:
            return bool(is_today_business_day())
        except Exception:
            day = day or dt.date.today()
            return day.weekday() < 5
    except Exception:
        day = day or dt.date.today()
        return day.weekday() < 5


def _get_prev_business_day(day: Optional[dt.date] = None) -> dt.date:
    try:
        return get_previous_business_day(day or dt.date.today())
    except TypeError:
        try:
            return get_previous_business_day()
        except Exception:
            day = day or dt.date.today()
            cur = day - dt.timedelta(days=1)
            while cur.weekday() >= 5:
                cur -= dt.timedelta(days=1)
            return cur
    except Exception:
        day = day or dt.date.today()
        cur = day - dt.timedelta(days=1)
        while cur.weekday() >= 5:
            cur -= dt.timedelta(days=1)
        return cur


def _today() -> dt.date:
    return dt.date.today()


def _now() -> dt.datetime:
    return dt.datetime.now()


def _time_str(t: dt.time) -> str:
    return t.strftime("%H:%M:%S")


def _date_str(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def _coerce_datetime(v: Any) -> Optional[pd.Timestamp]:
    try:
        x = pd.to_datetime(v, errors="coerce")
        if pd.isna(x):
            return None
        return pd.Timestamp(x)
    except Exception:
        return None


# ============================================================
# global_data slot
# ============================================================

def _ensure_global_slots() -> None:
    default_list_slots = [
        "runtime_symbols",
        "ranking_runtime_symbols",
        "summary_runtime_symbols",
        "margin_runtime_symbols",
        "short_sell_candidate_symbols",
        "buy_candidate_symbols",
        "should_register_symbols",
    ]
    for name in default_list_slots:
        if not hasattr(global_data, name):
            setattr(global_data, name, [])

    if not hasattr(global_data, "runtime_symbol_selector_last_window"):
        global_data.runtime_symbol_selector_last_window = None

    if not hasattr(global_data, "runtime_symbol_selector_last_count"):
        global_data.runtime_symbol_selector_last_count = 0

    if not hasattr(global_data, "runtime_symbol_selector_last_updated_at"):
        global_data.runtime_symbol_selector_last_updated_at = None


# ============================================================
# パス解決
# ============================================================

def _resolve_ranking_db_path(target_date: Optional[dt.date] = None) -> Optional[str]:
    target_date = target_date or _today()
    ymd = target_date.strftime("%Y%m%d")

    candidates: List[str] = []

    try:
        p = get_path("RANKING_DB")
        if p:
            candidates.append(str(p))
    except Exception:
        pass

    try:
        p = get_path("ranking_db")
        if p:
            candidates.append(str(p))
    except Exception:
        pass

    candidates.extend([
        rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking{ymd}.db",
        rf"Y:\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking{ymd}.db",
        rf"Y:\stock_ranking\ranking{ymd}.db",
    ])

    for p in candidates:
        try:
            if p and Path(p).exists():
                return p
        except Exception:
            continue

    return candidates[0] if candidates else None


def _resolve_summary_db_path(target_date: Optional[dt.date] = None) -> Optional[str]:
    target_date = target_date or _today()
    ymd = target_date.strftime("%Y%m%d")

    candidates: List[str] = []

    try:
        p = get_path("SUMMARY_DB")
        if p:
            candidates.append(str(p))
    except Exception:
        pass

    try:
        p = get_path("summary_db")
        if p:
            candidates.append(str(p))
    except Exception:
        pass

    candidates.extend([
        rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary\summary{ymd}.db",
        rf"Y:\AutoStockBuyAndSell\raw_data\kabu_station\summary\summary{ymd}.db",
    ])

    for p in candidates:
        try:
            if p and Path(p).exists():
                return p
        except Exception:
            continue

    return candidates[0] if candidates else None


def _resolve_symbol_flags_db() -> Optional[str]:
    candidates: List[str] = []

    try:
        p = get_path("SYMBOL_FLAGS_DB")
        if p:
            candidates.append(str(p))
    except Exception:
        pass

    try:
        p = get_path("symbol_flags_db")
        if p:
            candidates.append(str(p))
    except Exception:
        pass

    candidates.extend(DEFAULT_FLAG_DB_CANDIDATES)

    for p in candidates:
        try:
            if p and Path(p).exists():
                return p
        except Exception:
            continue

    return candidates[0] if candidates else None


# ============================================================
# DB読取
# ============================================================

def _connect_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)

    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass

    try:
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass

    try:
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass

    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    try:
        cur = conn.execute(f"PRAGMA table_info({table_name})")
        rows = cur.fetchall()
        return [str(r[1]) for r in rows if len(r) > 1]
    except Exception:
        return []


def _pick_existing_table(conn: sqlite3.Connection, candidates: Sequence[str]) -> Optional[str]:
    for t in candidates:
        if _table_exists(conn, t):
            return t
    return None


def _pick_column(columns: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    colset = {str(c).lower(): str(c) for c in columns}
    for cand in candidates:
        key = str(cand).lower()
        if key in colset:
            return colset[key]
    return None


# ============================================================
# 時間窓
# ============================================================

def build_runtime_window(now_dt: Optional[dt.datetime] = None) -> RuntimeWindow:
    now_dt = now_dt or _now()
    today = now_dt.date()

    if _is_business_day(today):
        prev_bd = _get_prev_business_day(today)
        return RuntimeWindow(
            date_from=_date_str(prev_bd),
            time_from=_time_str(MARKET_OPEN),
            date_to=_date_str(today),
            time_to=now_dt.strftime("%H:%M:%S"),
        )

    prev_bd = _get_prev_business_day(today)
    return RuntimeWindow(
        date_from=_date_str(prev_bd),
        time_from=_time_str(MARKET_OPEN),
        date_to=_date_str(prev_bd),
        time_to=_time_str(MARKET_CLOSE),
    )


# ============================================================
# symbol_flags 読込・フィルタ
# ============================================================

def _load_symbol_flags_df(db_path: Optional[str] = None) -> pd.DataFrame:
    db_path = db_path or _resolve_symbol_flags_db()

    if not db_path:
        logger.warning("[RUNTIME SELECTOR] symbol_flags DB path unresolved")
        return pd.DataFrame()

    if not Path(db_path).exists():
        logger.warning("[RUNTIME SELECTOR] symbol_flags DB not found: %s", db_path)
        return pd.DataFrame()

    try:
        with _connect_sqlite(db_path) as conn:
            if not _table_exists(conn, "symbol_flags"):
                logger.warning("[RUNTIME SELECTOR] table not found: symbol_flags")
                return pd.DataFrame()

            df = pd.read_sql("SELECT * FROM symbol_flags", conn)
            logger.info(
                "[RUNTIME SELECTOR] symbol_flags loaded rows=%s path=%s",
                len(df), db_path
            )
            return df

    except Exception:
        logger.exception("[RUNTIME SELECTOR] failed to load symbol_flags")
        return pd.DataFrame()


def _normalize_symbol_flags_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()

    if isinstance(work.columns, pd.MultiIndex):
        work.columns = [
            "_".join([str(x) for x in tup if str(x) != ""]).strip("_")
            for tup in work.columns
        ]

    work.columns = [str(c).strip() for c in work.columns]
    if work.columns.duplicated().any():
        dup = work.columns[work.columns.duplicated()].tolist()
        logger.warning("[RUNTIME SELECTOR] duplicate columns removed in symbol_flags: %s", dup)
        work = work.loc[:, ~work.columns.duplicated()]

    return work


def _apply_symbol_flags_filters(
    df: pd.DataFrame,
    require_margin: bool = False,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    work = _normalize_symbol_flags_df(df)

    cols = list(work.columns)
    symbol_col = _pick_column(cols, ["symbol", "code", "銘柄コード"])
    market_col = _pick_column(cols, ["market_type", "market", "市場", "市場区分"])
    name_col = _pick_column(cols, ["symbolname", "name", "銘柄名"])
    margin_col = _pick_column(cols, [
        "is_margin", "margin", "貸借", "loan_margin", "貸借銘柄",
        "margin_trade", "short_sell", "sellable",
    ])

    if not symbol_col:
        logger.warning("[RUNTIME SELECTOR] symbol column not found in symbol_flags")
        return pd.DataFrame()

    work[symbol_col] = work[symbol_col].map(_normalize_symbol)
    work = work[work[symbol_col] != ""].copy()

    if market_col and market_col in work.columns:
        work[market_col] = work[market_col].map(_safe_str)
        work = work[work[market_col].isin(ALLOWED_MARKET_TYPES)].copy()

    if name_col and name_col in work.columns:
        names = work[name_col].fillna("").astype(str)
        mask_ex = pd.Series(False, index=work.index)
        for kw in EXCLUDE_NAME_KEYWORDS:
            mask_ex = mask_ex | names.str.contains(kw, na=False, regex=False)
        work = work[~mask_ex].copy()

    if require_margin:
        if margin_col and margin_col in work.columns:
            work = work[work[margin_col].map(_safe_bool_from_any)].copy()
        else:
            logger.warning("[RUNTIME SELECTOR] require_margin=True but margin column not found")

    return work


def _filter_common_stock_symbols_from_flags(
    symbols: Sequence[Any],
    require_margin: bool = False,
    db_path: Optional[str] = None,
) -> List[str]:
    src = _dedupe_keep_order(symbols)
    if not src:
        return []

    df = _load_symbol_flags_df(db_path=db_path)
    if df.empty:
        logger.warning("[RUNTIME SELECTOR] symbol_flags empty -> raw symbols return")
        return src

    work = _apply_symbol_flags_filters(df, require_margin=require_margin)
    if work.empty:
        logger.warning("[RUNTIME SELECTOR] filtered symbol_flags empty require_margin=%s", require_margin)
        return []

    cols = list(work.columns)
    symbol_col = _pick_column(cols, ["symbol", "code", "銘柄コード"])
    if not symbol_col:
        return src

    allowed = set(work[symbol_col].tolist())
    out = [s for s in src if s in allowed]

    logger.info(
        "[RUNTIME SELECTOR] common stock filter: in=%s out=%s require_margin=%s",
        len(src), len(out), require_margin
    )
    return out


def load_jpx_common_stock_symbols(require_margin: bool = False) -> List[str]:
    df = _load_symbol_flags_df()
    if df.empty:
        return []

    work = _apply_symbol_flags_filters(df, require_margin=require_margin)
    if work.empty:
        return []

    cols = list(work.columns)
    symbol_col = _pick_column(cols, ["symbol", "code", "銘柄コード"])
    if not symbol_col:
        return []

    return _dedupe_keep_order(work[symbol_col].tolist())


# ============================================================
# ranking DB から runtime symbol 抽出
# ============================================================

def _query_symbols_from_ranking_db(
    db_path: str,
    window: RuntimeWindow,
    tables: Optional[Sequence[str]] = None,
) -> List[str]:
    if not db_path or not Path(db_path).exists():
        logger.warning("[RUNTIME SELECTOR] ranking db missing: %s", db_path)
        return []

    tables = list(tables or [
        "ranking_snapshot_1min",
        "ranking_raw_1min",
        "ranking_snapshot",
        "ranking_raw",
    ])

    try:
        with _connect_sqlite(db_path) as conn:
            table_name = _pick_existing_table(conn, tables)
            if not table_name:
                logger.warning("[RUNTIME SELECTOR] ranking table not found candidates=%s", tables)
                return []

            cols = _get_table_columns(conn, table_name)
            symbol_col = _pick_column(cols, ["symbol", "code"])
            dt_col = _pick_column(cols, ["datetime", "timestamp", "dt", "snapshot_time"])
            date_col = _pick_column(cols, ["date", "trade_date"])
            time_col = _pick_column(cols, ["time", "trade_time"])

            if not symbol_col:
                logger.warning("[RUNTIME SELECTOR] ranking symbol column not found table=%s", table_name)
                return []

            if dt_col:
                sql = f"""
                    SELECT DISTINCT {symbol_col} AS symbol
                    FROM {table_name}
                    WHERE {dt_col} >= ?
                      AND {dt_col} <= ?
                      AND {symbol_col} IS NOT NULL
                      AND TRIM({symbol_col}) <> ''
                """
                dt_from = f"{window.date_from} {window.time_from}"
                dt_to = f"{window.date_to} {window.time_to}"
                df = pd.read_sql(sql, conn, params=[dt_from, dt_to])

            elif date_col and time_col:
                sql = f"""
                    SELECT DISTINCT {symbol_col} AS symbol
                    FROM {table_name}
                    WHERE (
                        ({date_col} > ? OR ({date_col} = ? AND {time_col} >= ?))
                        AND
                        ({date_col} < ? OR ({date_col} = ? AND {time_col} <= ?))
                    )
                    AND {symbol_col} IS NOT NULL
                    AND TRIM({symbol_col}) <> ''
                """
                df = pd.read_sql(
                    sql,
                    conn,
                    params=[
                        window.date_from, window.date_from, window.time_from,
                        window.date_to, window.date_to, window.time_to,
                    ],
                )
            else:
                sql = f"""
                    SELECT DISTINCT {symbol_col} AS symbol
                    FROM {table_name}
                    WHERE {symbol_col} IS NOT NULL
                      AND TRIM({symbol_col}) <> ''
                """
                df = pd.read_sql(sql, conn)

            if df.empty or "symbol" not in df.columns:
                return []

            syms = _dedupe_keep_order(df["symbol"].tolist())
            logger.info(
                "[RUNTIME SELECTOR] ranking db symbols=%s table=%s window=%s",
                len(syms), table_name, window
            )
            return syms

    except Exception:
        logger.exception("[RUNTIME SELECTOR] ranking db query failed")
        return []


def load_runtime_symbols_from_ranking_db(
    target_date: Optional[dt.date] = None,
    require_margin: bool = False,
    window: Optional[RuntimeWindow] = None,
) -> List[str]:
    window = window or build_runtime_window()
    db_path = _resolve_ranking_db_path(target_date=target_date or _today())
    syms = _query_symbols_from_ranking_db(db_path, window)
    syms = _filter_common_stock_symbols_from_flags(syms, require_margin=require_margin)
    return syms


# ============================================================
# summary DB から runtime symbol 抽出
# ============================================================

def _query_symbols_from_summary_db(
    db_path: str,
    window: RuntimeWindow,
    tables: Optional[Sequence[str]] = None,
) -> List[str]:
    if not db_path or not Path(db_path).exists():
        logger.warning("[RUNTIME SELECTOR] summary db missing: %s", db_path)
        return []

    tables = list(tables or [
        "stock_summary_1min",
        "stock_summary_3min",
        "stock_summary_5min",
    ])

    out: List[str] = []

    try:
        with _connect_sqlite(db_path) as conn:
            for table_name in tables:
                if not _table_exists(conn, table_name):
                    continue

                cols = _get_table_columns(conn, table_name)
                symbol_col = _pick_column(cols, ["symbol", "code"])
                dt_col = _pick_column(cols, ["datetime", "timestamp", "dt"])
                date_col = _pick_column(cols, ["date", "trade_date"])
                time_col = _pick_column(cols, ["time", "trade_time", "end_time"])

                if not symbol_col:
                    continue

                try:
                    if dt_col:
                        sql = f"""
                            SELECT DISTINCT {symbol_col} AS symbol
                            FROM {table_name}
                            WHERE {dt_col} >= ?
                              AND {dt_col} <= ?
                              AND {symbol_col} IS NOT NULL
                              AND TRIM({symbol_col}) <> ''
                        """
                        dt_from = f"{window.date_from} {window.time_from}"
                        dt_to = f"{window.date_to} {window.time_to}"
                        df = pd.read_sql(sql, conn, params=[dt_from, dt_to])

                    elif date_col and time_col:
                        sql = f"""
                            SELECT DISTINCT {symbol_col} AS symbol
                            FROM {table_name}
                            WHERE (
                                ({date_col} > ? OR ({date_col} = ? AND {time_col} >= ?))
                                AND
                                ({date_col} < ? OR ({date_col} = ? AND {time_col} <= ?))
                            )
                            AND {symbol_col} IS NOT NULL
                            AND TRIM({symbol_col}) <> ''
                        """
                        df = pd.read_sql(
                            sql,
                            conn,
                            params=[
                                window.date_from, window.date_from, window.time_from,
                                window.date_to, window.date_to, window.time_to,
                            ],
                        )
                    else:
                        continue

                    if not df.empty and "symbol" in df.columns:
                        out.extend(df["symbol"].tolist())

                except Exception:
                    logger.exception("[RUNTIME SELECTOR] summary query failed table=%s", table_name)
                    continue

        syms = _dedupe_keep_order(out)
        logger.info(
            "[RUNTIME SELECTOR] summary db symbols=%s tables=%s window=%s",
            len(syms), tables, window
        )
        return syms

    except Exception:
        logger.exception("[RUNTIME SELECTOR] summary db query failed")
        return []


def load_runtime_symbols_from_summary_db(
    target_date: Optional[dt.date] = None,
    require_margin: bool = False,
    window: Optional[RuntimeWindow] = None,
) -> List[str]:
    window = window or build_runtime_window()
    db_path = _resolve_summary_db_path(target_date=target_date or _today())
    syms = _query_symbols_from_summary_db(db_path, window)
    syms = _filter_common_stock_symbols_from_flags(syms, require_margin=require_margin)
    return syms


# ============================================================
# global_data 読取
# ============================================================

def _get_global_list(name: str) -> List[str]:
    try:
        v = getattr(global_data, name, None)
        if v is None:
            return []

        if isinstance(v, pd.DataFrame):
            for col in ["symbol", "code"]:
                if col in v.columns:
                    return _dedupe_keep_order(v[col].tolist())
            return []

        if isinstance(v, (list, tuple, set)):
            return _dedupe_keep_order(list(v))

        return []
    except Exception:
        return []


def load_runtime_symbols_from_global_data(
    require_margin: bool = False,
) -> List[str]:
    candidates: List[str] = []

    names = [
        "runtime_symbols",
        "ranking_runtime_symbols",
        "summary_runtime_symbols",
        "should_register_symbols",
        "ats_register_targets",
        "ats_targets",
        "push_symbols",
        "active_symbols",
        "watch_symbols",
        "monitor_symbols",
        "buy_candidate_symbols",
    ]

    for name in names:
        vals = _get_global_list(name)
        if vals:
            logger.info("[RUNTIME SELECTOR] global_data source=%s count=%s", name, len(vals))
            candidates.extend(vals)

    out = _dedupe_keep_order(candidates)
    out = _filter_common_stock_symbols_from_flags(out, require_margin=require_margin)
    return out


# ============================================================
# 統合 selector
# ============================================================

def select_runtime_symbols(
    *,
    use_global_data: bool = True,
    use_ranking_db: bool = True,
    use_summary_db: bool = True,
    require_margin: bool = False,
    save_to_global: bool = True,
    save_margin_to_global: bool = True,
) -> List[str]:
    _ensure_global_slots()

    merged: List[str] = []
    window = build_runtime_window()

    if use_global_data:
        try:
            merged.extend(load_runtime_symbols_from_global_data(require_margin=False))
        except Exception:
            logger.exception("[RUNTIME SELECTOR] load from global_data failed")

    if use_ranking_db:
        try:
            merged.extend(
                load_runtime_symbols_from_ranking_db(
                    require_margin=False,
                    window=window,
                )
            )
        except Exception:
            logger.exception("[RUNTIME SELECTOR] load from ranking db failed")

    if use_summary_db:
        try:
            merged.extend(
                load_runtime_symbols_from_summary_db(
                    require_margin=False,
                    window=window,
                )
            )
        except Exception:
            logger.exception("[RUNTIME SELECTOR] load from summary db failed")

    merged = _dedupe_keep_order(merged)
    merged = _filter_common_stock_symbols_from_flags(merged, require_margin=False)

    margin_symbols: List[str] = []
    if require_margin:
        margin_symbols = _filter_common_stock_symbols_from_flags(merged, require_margin=True)
        merged = margin_symbols

    if save_to_global:
        try:
            setattr(global_data, "runtime_symbols", merged)
            setattr(global_data, "ranking_runtime_symbols", merged)
            setattr(global_data, "should_register_symbols", merged)
            setattr(global_data, "buy_candidate_symbols", merged)
            logger.info(
                "[RUNTIME SELECTOR] saved runtime_symbols to global_data count=%s",
                len(merged)
            )
        except Exception:
            logger.exception("[RUNTIME SELECTOR] save runtime_symbols to global_data failed")

    if save_margin_to_global:
        try:
            if not margin_symbols:
                margin_symbols = _filter_common_stock_symbols_from_flags(merged, require_margin=True)

            setattr(global_data, "margin_runtime_symbols", margin_symbols)
            setattr(global_data, "short_sell_candidate_symbols", margin_symbols)

            logger.info(
                "[RUNTIME SELECTOR] saved margin_runtime_symbols to global_data count=%s",
                len(margin_symbols)
            )
        except Exception:
            logger.exception("[RUNTIME SELECTOR] save margin symbols to global_data failed")

    try:
        global_data.runtime_symbol_selector_last_window = {
            "date_from": window.date_from,
            "time_from": window.time_from,
            "date_to": window.date_to,
            "time_to": window.time_to,
        }
        global_data.runtime_symbol_selector_last_count = len(merged)
        global_data.runtime_symbol_selector_last_updated_at = _now()
    except Exception:
        logger.exception("[RUNTIME SELECTOR] save selector meta failed")

    logger.info(
        "[RUNTIME SELECTOR] final selected count=%s require_margin=%s window=%s",
        len(merged), require_margin, window
    )
    return merged


def select_buy_symbols() -> List[str]:
    return select_runtime_symbols(require_margin=False)


def select_short_sell_symbols() -> List[str]:
    return select_runtime_symbols(require_margin=True)


# ============================================================
# ステータス / デバッグ
# ============================================================

def get_runtime_symbol_selector_status() -> dict[str, Any]:
    _ensure_global_slots()

    try:
        runtime_symbols = _get_global_list("runtime_symbols")
        margin_symbols = _get_global_list("margin_runtime_symbols")

        return {
            "runtime_count": len(runtime_symbols),
            "margin_count": len(margin_symbols),
            "last_window": getattr(global_data, "runtime_symbol_selector_last_window", None),
            "last_count": getattr(global_data, "runtime_symbol_selector_last_count", 0),
            "last_updated_at": getattr(global_data, "runtime_symbol_selector_last_updated_at", None),
        }
    except Exception:
        logger.exception("[RUNTIME SELECTOR] status failed")
        return {}


def debug_print_selected_symbols(limit: int = 100) -> None:
    try:
        syms = _get_global_list("runtime_symbols")
        margin_syms = _get_global_list("margin_runtime_symbols")

        print("\n# ============================================================")
        print("# RUNTIME SYMBOL SELECTOR RESULT")
        print("# ============================================================")
        print(f"runtime_symbols       : {len(syms)}")
        if syms:
            print(", ".join(syms[:limit]))

        print(f"margin_runtime_symbols: {len(margin_syms)}")
        if margin_syms:
            print(", ".join(margin_syms[:limit]))

        print("# ============================================================\n")
    except Exception:
        logger.exception("[RUNTIME SELECTOR] debug print failed")


__all__ = [
    "RuntimeWindow",
    "build_runtime_window",
    "load_jpx_common_stock_symbols",
    "load_runtime_symbols_from_ranking_db",
    "load_runtime_symbols_from_summary_db",
    "load_runtime_symbols_from_global_data",
    "select_runtime_symbols",
    "select_buy_symbols",
    "select_short_sell_symbols",
    "get_runtime_symbol_selector_status",
    "debug_print_selected_symbols",
]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    selected = select_runtime_symbols(require_margin=False)
    logger.info("selected=%s", len(selected))

    short_selected = select_runtime_symbols(require_margin=True)
    logger.info("short_selected=%s", len(short_selected))

    debug_print_selected_symbols()