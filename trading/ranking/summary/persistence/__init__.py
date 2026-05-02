# ============================================================
# File   : trading/ranking/summary/persistence/__init__.py
# Version: PRODUCTION-COMPAT-RANKING-SUMMARY-PERSISTENCE-EXPORT-V5
# ------------------------------------------------------------
# Purpose:
#   trading.ranking.summary.__init__ / runner / bootstrap / announce
#   から参照される persistence API を互換 export する。
#
# Fix:
#   ImportError:
#     cannot import name 'save_ranking_summary'
#     cannot import name 'load_latest_ranking_summary'
#     cannot import name 'ensure_ranking_summary_table'
#     from trading.ranking.summary.persistence
#
# Policy:
#   - 既存実装があればそれを優先
#   - なければ summary DB に ranking_summary_Xmin テーブルを安全作成
#   - save/load/ensure の import 自体は絶対に落とさない
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# constants
# ============================================================

DEFAULT_NAS_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell"

_SAVE_BACKEND_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("trading.ranking.summary.persistence.saver", "save_ranking_summary"),
    ("trading.ranking.summary.persistence.writer", "save_ranking_summary"),
    ("trading.ranking.summary.persistence.repository", "save_ranking_summary"),
    ("trading.ranking.summary.persistence.saver", "save_ranking_summary_df"),
    ("trading.ranking.summary.persistence.writer", "save_ranking_summary_df"),
    ("trading.ranking.summary.persistence.repository", "save_ranking_summary_df"),
    ("database.crud.crud_ranking_summary", "insert_ranking_summary_1min"),
    ("database.crud.crud_ranking_summary", "save_ranking_summary_1min"),
)

_LOAD_BACKEND_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("trading.ranking.summary.persistence.loader", "load_latest_ranking_summary"),
    ("trading.ranking.summary.persistence.reader", "load_latest_ranking_summary"),
    ("trading.ranking.summary.persistence.repository", "load_latest_ranking_summary"),
    ("trading.ranking.summary.persistence.loader", "load_ranking_summary"),
    ("trading.ranking.summary.persistence.reader", "load_ranking_summary"),
    ("trading.ranking.summary.persistence.repository", "load_ranking_summary"),
)

_ENSURE_BACKEND_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("trading.ranking.summary.persistence.saver", "ensure_ranking_summary_table"),
    ("trading.ranking.summary.persistence.writer", "ensure_ranking_summary_table"),
    ("trading.ranking.summary.persistence.repository", "ensure_ranking_summary_table"),
    ("database.crud.crud_ranking_summary", "ensure_ranking_summary_table"),
    ("database.crud.crud_ranking_summary", "ensure_table"),
)

_SAVE_BACKEND_CACHE: Optional[Callable[..., Any]] = None
_LOAD_BACKEND_CACHE: Optional[Callable[..., Any]] = None
_ENSURE_BACKEND_CACHE: Optional[Callable[..., Any]] = None

_SCHEMA_ENSURED_TABLES: set[tuple[str, str]] = set()


# ============================================================
# basic helpers
# ============================================================

def _safe_len(obj: Any) -> int:
    try:
        return int(len(obj))
    except Exception:
        return 0


def _is_empty_df(df: Any) -> bool:
    try:
        return bool(getattr(df, "empty", False))
    except Exception:
        return False


def _env_path(name: str) -> Optional[Path]:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return Path(v)
    except Exception:
        return None


def _nas_root() -> Path:
    for key in ("NAS_ROOT", "AUTOSTOCK_NAS_ROOT", "KABU_NAS_ROOT"):
        p = _env_path(key)
        if p is not None:
            return p
    return Path(DEFAULT_NAS_ROOT)


def _yyyymmdd(value: Any = None) -> str:
    if value is None:
        return dt.datetime.now().strftime("%Y%m%d")

    try:
        if isinstance(value, pd.Timestamp):
            if pd.isna(value):
                return dt.datetime.now().strftime("%Y%m%d")
            return value.strftime("%Y%m%d")
    except Exception:
        pass

    try:
        if isinstance(value, dt.datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, dt.date):
            return value.strftime("%Y%m%d")
    except Exception:
        pass

    s = str(value).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]

    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path(date_yyyymmdd: Any = None) -> Path:
    ymd = _yyyymmdd(date_yyyymmdd)
    return _nas_root() / "raw_data" / "kabu_station" / "summary" / f"summary{ymd}.db"


def _ranking_db_path(date_yyyymmdd: Any = None) -> Path:
    ymd = _yyyymmdd(date_yyyymmdd)
    return _nas_root() / "raw_data" / "kabu_station" / "ranking" / f"ranking{ymd}.db"


def _table_name_for_interval(interval: int | str) -> str:
    i = int(interval)
    return f"ranking_summary_{i}min"


def _fallback_summary_table_name_for_interval(interval: int | str) -> str:
    i = int(interval)
    return f"stock_summary_{i}min"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _safe_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


# ============================================================
# local table ensure
# ============================================================

def _ensure_local_ranking_summary_table(
    *,
    interval: int | str = 1,
    date_yyyymmdd: Any = None,
    db_path: Optional[str | Path] = None,
) -> bool:
    """
    summaryYYYYMMDD.db に ranking_summary_Xmin テーブルを作成する。
    既存DB非破壊、ADD ONLY。
    """
    interval = int(interval)
    table = _table_name_for_interval(interval)

    path = Path(db_path) if db_path else _summary_db_path(date_yyyymmdd)
    path.parent.mkdir(parents=True, exist_ok=True)

    cache_key = (str(path), table)
    if cache_key in _SCHEMA_ENSURED_TABLES:
        return True

    try:
        with sqlite3.connect(str(path), timeout=15) as conn:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_safe_ident(table)} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    symbolname TEXT,
                    datetime TEXT NOT NULL,
                    date TEXT,
                    time TEXT,
                    time_range TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    open_price REAL,
                    high_price REAL,
                    low_price REAL,
                    close_price REAL,
                    volume REAL,
                    best_rank INTEGER,
                    rank INTEGER,
                    ranking_type TEXT,
                    rank_type TEXT,
                    market TEXT,
                    price REAL,
                    current_price REAL,
                    change_rate REAL,
                    change_percentage REAL,
                    change_ratio REAL,
                    trading_volume REAL,
                    trading_value REAL,
                    turnover REAL,
                    tick_count INTEGER,
                    ma5 REAL,
                    ma25 REAL,
                    ma75 REAL,
                    rsi REAL,
                    macd REAL,
                    signal REAL,
                    hist REAL,
                    slope REAL,
                    slope_atr_scaled REAL,
                    mtf REAL,
                    score_mtf REAL,
                    score_slope REAL,
                    score REAL,
                    score_buy REAL,
                    score_sell REAL,
                    score_total REAL,
                    final_score REAL,
                    display_score REAL,
                    source TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

            conn.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS {_safe_ident(f'uq_{table}_symbol_datetime')}
                ON {_safe_ident(table)} (symbol, datetime)
                """
            )

            conn.commit()

        _SCHEMA_ENSURED_TABLES.add(cache_key)

        logger.info(
            "[ranking.summary.persistence] ensured table db=%s table=%s",
            path,
            table,
        )
        return True

    except Exception:
        logger.exception(
            "[ranking.summary.persistence] ensure local table failed db=%s table=%s",
            path,
            table,
        )
        return False


def ensure_ranking_summary_table(
    interval: int | str = 1,
    *args: Any,
    date_yyyymmdd: Any = None,
    db_path: Optional[str | Path] = None,
    **kwargs: Any,
) -> bool:
    """
    ranking_summary_Xmin テーブル存在保証の互換入口。

    trading.ranking.summary.__init__ から import されるため必須。
    """
    interval = int(interval)

    backend = _resolve_backend(_ENSURE_BACKEND_CANDIDATES, cache_kind="ensure")
    if callable(backend):
        ret = _call_backend_safely(
            backend,
            interval=interval,
            date_yyyymmdd=date_yyyymmdd,
            db_path=db_path,
            **kwargs,
        )
        if ret is not None:
            try:
                return bool(ret)
            except Exception:
                return True

    return _ensure_local_ranking_summary_table(
        interval=interval,
        date_yyyymmdd=date_yyyymmdd,
        db_path=db_path,
    )


def ensure_table(
    interval: int | str = 1,
    *args: Any,
    **kwargs: Any,
) -> bool:
    return ensure_ranking_summary_table(interval=interval, *args, **kwargs)


def ensure_tables(
    intervals: tuple[int, ...] | list[int] = (1, 3, 5),
    *args: Any,
    **kwargs: Any,
) -> bool:
    ok = True
    for interval in intervals:
        ok = ensure_ranking_summary_table(interval=interval, *args, **kwargs) and ok
    return ok


# ============================================================
# db readers
# ============================================================

def _read_latest_rows_from_table(
    db_path: Path,
    table: str,
    *,
    limit: int = 5000,
    source_like: Optional[str] = None,
) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()

    try:
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")

            if not _table_exists(conn, table):
                return pd.DataFrame()

            where = "WHERE datetime IS NOT NULL"
            params: list[Any] = []

            if source_like:
                where += " AND source LIKE ?"
                params.append(f"%{source_like}%")

            sql = f"""
                WITH latest_dt AS (
                    SELECT MAX(datetime) AS max_dt
                    FROM {_safe_ident(table)}
                    {where}
                )
                SELECT *
                FROM {_safe_ident(table)}
                WHERE datetime = (SELECT max_dt FROM latest_dt)
                LIMIT ?
            """
            params.append(int(limit))

            return pd.read_sql_query(sql, conn, params=params)

    except Exception:
        logger.debug(
            "[ranking.summary.persistence] read latest failed db=%s table=%s",
            db_path,
            table,
            exc_info=True,
        )
        return pd.DataFrame()


def _read_recent_rows_from_table(
    db_path: Path,
    table: str,
    *,
    limit: int = 5000,
    source_like: Optional[str] = None,
) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()

    try:
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")

            if not _table_exists(conn, table):
                return pd.DataFrame()

            where = "WHERE datetime IS NOT NULL"
            params: list[Any] = []

            if source_like:
                where += " AND source LIKE ?"
                params.append(f"%{source_like}%")

            sql = f"""
                SELECT *
                FROM {_safe_ident(table)}
                {where}
                ORDER BY datetime DESC
                LIMIT ?
            """
            params.append(int(limit))

            return pd.read_sql_query(sql, conn, params=params)

    except Exception:
        logger.debug(
            "[ranking.summary.persistence] read recent failed db=%s table=%s",
            db_path,
            table,
            exc_info=True,
        )
        return pd.DataFrame()


def _normalize_loaded_df(df: Any) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()
        if isinstance(df, pd.DataFrame):
            out = df.copy()
        else:
            out = pd.DataFrame(df)

        if out.empty:
            return out

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")

        if "symbol" in out.columns:
            out["symbol"] = (
                out["symbol"]
                .astype(str)
                .str.replace(r"\.0$", "", regex=True)
                .str.strip()
            )

        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[ranking.summary.persistence] normalize loaded df failed", exc_info=True)
        return pd.DataFrame()


# ============================================================
# backend resolver
# ============================================================

def _resolve_backend(
    candidates: tuple[tuple[str, str], ...],
    *,
    cache_kind: str,
) -> Optional[Callable[..., Any]]:
    global _SAVE_BACKEND_CACHE
    global _LOAD_BACKEND_CACHE
    global _ENSURE_BACKEND_CACHE

    if cache_kind == "save" and callable(_SAVE_BACKEND_CACHE):
        return _SAVE_BACKEND_CACHE

    if cache_kind == "load" and callable(_LOAD_BACKEND_CACHE):
        return _LOAD_BACKEND_CACHE

    if cache_kind == "ensure" and callable(_ENSURE_BACKEND_CACHE):
        return _ENSURE_BACKEND_CACHE

    for module_name, func_name in candidates:
        try:
            if module_name == __name__:
                continue

            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)

            if callable(fn):
                if cache_kind == "save":
                    _SAVE_BACKEND_CACHE = fn
                elif cache_kind == "load":
                    _LOAD_BACKEND_CACHE = fn
                elif cache_kind == "ensure":
                    _ENSURE_BACKEND_CACHE = fn

                logger.info(
                    "[ranking.summary.persistence] %s backend resolved %s.%s",
                    cache_kind,
                    module_name,
                    func_name,
                )
                return fn

        except Exception:
            logger.debug(
                "[ranking.summary.persistence] %s backend resolve failed %s.%s",
                cache_kind,
                module_name,
                func_name,
                exc_info=True,
            )

    return None


def _call_backend_safely(
    backend: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if not callable(backend):
        return None

    try:
        sig = inspect.signature(backend)
        params = sig.parameters

        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )

        if accepts_var_kw:
            return backend(*args, **kwargs)

        call_kwargs = {
            k: v
            for k, v in kwargs.items()
            if k in params
        }

        return backend(*args, **call_kwargs)

    except TypeError:
        try:
            return backend(*args)
        except Exception:
            logger.exception(
                "[ranking.summary.persistence] backend call failed backend=%s",
                getattr(backend, "__name__", repr(backend)),
            )
            return None

    except Exception:
        logger.exception(
            "[ranking.summary.persistence] backend call failed backend=%s",
            getattr(backend, "__name__", repr(backend)),
        )
        return None


def _normalize_save_return(ret: Any, df: Any) -> int:
    if ret is None:
        return _safe_len(df)

    if isinstance(ret, dict):
        for key in (
            "saved_rows",
            "inserted_rows",
            "inserted",
            "rows",
            "count",
            "saved",
        ):
            if key in ret:
                try:
                    return int(ret.get(key) or 0)
                except Exception:
                    pass

        try:
            if ret.get("ok") is True:
                return _safe_len(df)
        except Exception:
            pass

        return 0

    try:
        return int(ret)
    except Exception:
        return _safe_len(df)


# ============================================================
# save api
# ============================================================

def save_ranking_summary(
    df: Optional[pd.DataFrame],
    *args: Any,
    interval: int | str = 1,
    source: str = "ranking",
    **kwargs: Any,
) -> int:
    """
    ランキングサマリー保存の互換入口。
    """
    if df is None:
        logger.warning("[ranking.summary.persistence] save skipped df=None")
        return 0

    if _is_empty_df(df):
        logger.info(
            "[ranking.summary.persistence] save skipped empty df interval=%s",
            interval,
        )
        return 0

    interval = int(interval)
    ensure_ranking_summary_table(interval=interval)

    backend = _resolve_backend(_SAVE_BACKEND_CANDIDATES, cache_kind="save")

    if not callable(backend):
        logger.warning(
            "[ranking.summary.persistence] no save backend available; skipped interval=%s rows=%s",
            interval,
            _safe_len(df),
        )
        return 0

    call_kwargs = dict(kwargs)
    call_kwargs.setdefault("interval", interval)
    call_kwargs.setdefault("source", source)

    if args:
        logger.debug(
            "[ranking.summary.persistence] positional args ignored for save compat count=%s",
            len(args),
        )

    ret = _call_backend_safely(
        backend,
        df,
        **call_kwargs,
    )

    saved = _normalize_save_return(ret, df)

    logger.info(
        "[ranking.summary.persistence] save done interval=%s source=%s rows=%s saved=%s backend=%s",
        interval,
        source,
        _safe_len(df),
        saved,
        getattr(backend, "__name__", repr(backend)),
    )

    return saved


def save_ranking_summary_df(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    return save_ranking_summary(df, *args, **kwargs)


def persist_ranking_summary(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    return save_ranking_summary(df, *args, **kwargs)


def persist_ranking_summary_df(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    return save_ranking_summary(df, *args, **kwargs)


def save(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    return save_ranking_summary(df, *args, **kwargs)


def save_df(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    return save_ranking_summary(df, *args, **kwargs)


# ============================================================
# load api
# ============================================================

def load_latest_ranking_summary(
    interval: int | str = 1,
    *args: Any,
    limit: int = 5000,
    date_yyyymmdd: Any = None,
    **kwargs: Any,
) -> pd.DataFrame:
    """
    最新ランキングサマリーを読み込む互換入口。
    """
    interval = int(interval)

    backend = _resolve_backend(_LOAD_BACKEND_CANDIDATES, cache_kind="load")
    if callable(backend):
        ret = _call_backend_safely(
            backend,
            interval=interval,
            limit=limit,
            date_yyyymmdd=date_yyyymmdd,
            **kwargs,
        )
        df = _normalize_loaded_df(ret)
        if not df.empty:
            logger.info(
                "[ranking.summary.persistence] load latest via backend interval=%s rows=%s",
                interval,
                len(df),
            )
            return df

    summary_db = _summary_db_path(date_yyyymmdd)
    ranking_db = _ranking_db_path(date_yyyymmdd)

    table = _table_name_for_interval(interval)
    df = _read_latest_rows_from_table(summary_db, table, limit=limit)
    df = _normalize_loaded_df(df)
    if not df.empty:
        logger.info(
            "[ranking.summary.persistence] load latest summary_db table=%s rows=%s",
            table,
            len(df),
        )
        return df

    fallback_table = _fallback_summary_table_name_for_interval(interval)
    df = _read_latest_rows_from_table(
        summary_db,
        fallback_table,
        limit=limit,
        source_like="ranking",
    )
    df = _normalize_loaded_df(df)
    if not df.empty:
        logger.info(
            "[ranking.summary.persistence] load latest summary_db fallback table=%s rows=%s",
            fallback_table,
            len(df),
        )
        return df

    df = _read_latest_rows_from_table(ranking_db, table, limit=limit)
    df = _normalize_loaded_df(df)
    if not df.empty:
        logger.info(
            "[ranking.summary.persistence] load latest ranking_db table=%s rows=%s",
            table,
            len(df),
        )
        return df

    logger.info(
        "[ranking.summary.persistence] load latest empty interval=%s summary_db=%s ranking_db=%s",
        interval,
        summary_db,
        ranking_db,
    )
    return pd.DataFrame()


def load_ranking_summary(
    interval: int | str = 1,
    *args: Any,
    limit: int = 5000,
    date_yyyymmdd: Any = None,
    latest_only: bool = True,
    **kwargs: Any,
) -> pd.DataFrame:
    """
    ランキングサマリー読み込み互換入口。
    """
    if latest_only:
        return load_latest_ranking_summary(
            interval=interval,
            limit=limit,
            date_yyyymmdd=date_yyyymmdd,
            **kwargs,
        )

    interval = int(interval)
    summary_db = _summary_db_path(date_yyyymmdd)
    ranking_db = _ranking_db_path(date_yyyymmdd)

    table = _table_name_for_interval(interval)
    df = _read_recent_rows_from_table(summary_db, table, limit=limit)
    df = _normalize_loaded_df(df)
    if not df.empty:
        return df

    fallback_table = _fallback_summary_table_name_for_interval(interval)
    df = _read_recent_rows_from_table(
        summary_db,
        fallback_table,
        limit=limit,
        source_like="ranking",
    )
    df = _normalize_loaded_df(df)
    if not df.empty:
        return df

    df = _read_recent_rows_from_table(ranking_db, table, limit=limit)
    return _normalize_loaded_df(df)


def load_latest(
    interval: int | str = 1,
    *args: Any,
    **kwargs: Any,
) -> pd.DataFrame:
    return load_latest_ranking_summary(interval=interval, *args, **kwargs)


def load(
    interval: int | str = 1,
    *args: Any,
    **kwargs: Any,
) -> pd.DataFrame:
    return load_ranking_summary(interval=interval, *args, **kwargs)


def get_latest_ranking_summary(
    interval: int | str = 1,
    *args: Any,
    **kwargs: Any,
) -> pd.DataFrame:
    return load_latest_ranking_summary(interval=interval, *args, **kwargs)


# ============================================================
# exports
# ============================================================

__all__ = [
    "save_ranking_summary",
    "save_ranking_summary_df",
    "persist_ranking_summary",
    "persist_ranking_summary_df",
    "save",
    "save_df",
    "ensure_ranking_summary_table",
    "ensure_table",
    "ensure_tables",
    "load_latest_ranking_summary",
    "load_ranking_summary",
    "load_latest",
    "load",
    "get_latest_ranking_summary",
]