# ============================================================
# File   : database/crud/crud_ranking_raw.py
# Version: Ver1.5-PRODUCTION-HARDENED-RANKING-RAW-SCHEMA-CACHE
# ------------------------------------------------------------
# ✔ ranking_raw_1min 保存
# ✔ sqlite bind 安全化
# ✔ pandas.Timestamp / datetime / NaT 対応
# ✔ 旧呼び出し互換
# ✔ legacy UNIQUE(symbol, snapshot_time) 自動検出
# ✔ テーブル再作成マイグレーション
# ✔ 新UNIQUE(symbol, snapshot_time, rank_type_id, market)
# ✔ batch内 duplicate 除去
# ✔ fallback INSERT OR IGNORE
# ✔ production hardened
# ✔ change_percentage / change_ratio / turnover 保存対応
# ✔ inserted_at 保存対応
# ✔ 旧テーブルからの移行時に列追加
# ✔ database engine resolver not found を回避
# ✔ rankingYYYYMMDD.db を自力解決して SQLAlchemy engine fallback 作成
# ✔ NAS_ROOT / AUTOSTOCK_NAS_ROOT / KABU_NAS_ROOT 対応
# ✔ 旧テーブル移行時、存在しない列は NULL で安全コピー
# ✔ SQLite WAL / busy_timeout best-effort 設定
# ✔ NEW: schema / unique index ensure を DBファイル単位でキャッシュ
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import text

logger = logging.getLogger(__name__)

TABLE_NAME = "ranking_raw_1min"
TMP_TABLE_NAME = "ranking_raw_1min__new"


# ------------------------------------------------------------
# constants
# ------------------------------------------------------------

DEFAULT_NAS_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell"

_ENGINE_CACHE: Dict[str, Any] = {}

# schema ensure を毎回実行しないためのキャッシュ
# key は SQLite DB の実ファイルパス
_SCHEMA_ENSURED_DB_KEYS: set[str] = set()

_EXPECTED_COLUMNS = [
    "symbol",
    "snapshot_time",
    "symbolname",
    "rank_type",
    "rank_type_id",
    "market",
    "rank_position",
    "current_price",
    "change_percentage",
    "change_ratio",
    "trading_volume",
    "trading_value",
    "turnover",
    "tick_count",
    "volume_speed",
    "price_delta_1m",
    "volume_delta_1m",
    "minute_of_day",
    "source",
    "inserted_at",
    "created_at",
]


# ------------------------------------------------------------
# SQL
# ------------------------------------------------------------

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snapshot_time TEXT NOT NULL,
    symbolname TEXT,
    rank_type TEXT,
    rank_type_id INTEGER,
    market TEXT,
    rank_position INTEGER,
    current_price REAL,
    change_percentage REAL,
    change_ratio REAL,
    trading_volume REAL,
    trading_value REAL,
    turnover REAL,
    tick_count INTEGER,
    volume_speed REAL,
    price_delta_1m REAL,
    volume_delta_1m REAL,
    minute_of_day INTEGER,
    source TEXT,
    inserted_at TEXT,
    created_at TEXT
)
"""

CREATE_TMP_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TMP_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    snapshot_time TEXT NOT NULL,
    symbolname TEXT,
    rank_type TEXT,
    rank_type_id INTEGER,
    market TEXT,
    rank_position INTEGER,
    current_price REAL,
    change_percentage REAL,
    change_ratio REAL,
    trading_volume REAL,
    trading_value REAL,
    turnover REAL,
    tick_count INTEGER,
    volume_speed REAL,
    price_delta_1m REAL,
    volume_delta_1m REAL,
    minute_of_day INTEGER,
    source TEXT,
    inserted_at TEXT,
    created_at TEXT
)
"""

CREATE_UNIQUE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE_NAME}_unique
ON {TABLE_NAME}(symbol, snapshot_time, rank_type_id, market)
"""

CREATE_TMP_UNIQUE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS idx_{TMP_TABLE_NAME}_unique
ON {TMP_TABLE_NAME}(symbol, snapshot_time, rank_type_id, market)
"""

INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (
    symbol,
    snapshot_time,
    symbolname,
    rank_type,
    rank_type_id,
    market,
    rank_position,
    current_price,
    change_percentage,
    change_ratio,
    trading_volume,
    trading_value,
    turnover,
    tick_count,
    volume_speed,
    price_delta_1m,
    volume_delta_1m,
    minute_of_day,
    source,
    inserted_at,
    created_at
)
VALUES (
    :symbol,
    :snapshot_time,
    :symbolname,
    :rank_type,
    :rank_type_id,
    :market,
    :rank_position,
    :current_price,
    :change_percentage,
    :change_ratio,
    :trading_volume,
    :trading_value,
    :turnover,
    :tick_count,
    :volume_speed,
    :price_delta_1m,
    :volume_delta_1m,
    :minute_of_day,
    :source,
    :inserted_at,
    :created_at
)
ON CONFLICT(symbol, snapshot_time, rank_type_id, market) DO NOTHING
"""

INSERT_OR_IGNORE_SQL = f"""
INSERT OR IGNORE INTO {TABLE_NAME} (
    symbol,
    snapshot_time,
    symbolname,
    rank_type,
    rank_type_id,
    market,
    rank_position,
    current_price,
    change_percentage,
    change_ratio,
    trading_volume,
    trading_value,
    turnover,
    tick_count,
    volume_speed,
    price_delta_1m,
    volume_delta_1m,
    minute_of_day,
    source,
    inserted_at,
    created_at
)
VALUES (
    :symbol,
    :snapshot_time,
    :symbolname,
    :rank_type,
    :rank_type_id,
    :market,
    :rank_position,
    :current_price,
    :change_percentage,
    :change_ratio,
    :trading_volume,
    :trading_value,
    :turnover,
    :tick_count,
    :volume_speed,
    :price_delta_1m,
    :volume_delta_1m,
    :minute_of_day,
    :source,
    :inserted_at,
    :created_at
)
"""


# ------------------------------------------------------------
# path / engine resolver
# ------------------------------------------------------------

def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _normalize_yyyymmdd(value: Optional[Any] = None) -> str:
    if value is None:
        return _today_yyyymmdd()

    try:
        if isinstance(value, pd.Timestamp):
            if pd.isna(value):
                return _today_yyyymmdd()
            return value.strftime("%Y%m%d")
    except Exception:
        pass

    try:
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, date):
            return value.strftime("%Y%m%d")
    except Exception:
        pass

    s = str(value).strip()
    if not s:
        return _today_yyyymmdd()

    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]

    return _today_yyyymmdd()


def _get_nas_root() -> Path:
    """
    NAS root を安全に解決する。

    優先順位:
      1. NAS_ROOT
      2. AUTOSTOCK_NAS_ROOT
      3. KABU_NAS_ROOT
      4. DEFAULT_NAS_ROOT
    """
    for key in ("NAS_ROOT", "AUTOSTOCK_NAS_ROOT", "KABU_NAS_ROOT"):
        value = os.environ.get(key)
        if value:
            return Path(value)

    return Path(DEFAULT_NAS_ROOT)


def _resolve_ranking_db_path(date_yyyymmdd: Optional[Any] = None) -> Path:
    """
    rankingYYYYMMDD.db のパスを解決する。

    例:
      \\192.168.0.22\AutoStockBuyAndSell
        \raw_data\kabu_station\ranking\ranking20260430.db
    """
    ymd = _normalize_yyyymmdd(date_yyyymmdd)
    nas_root = _get_nas_root()

    return (
        nas_root
        / "raw_data"
        / "kabu_station"
        / "ranking"
        / f"ranking{ymd}.db"
    )


def _infer_date_yyyymmdd_from_rows(rows: Any) -> Optional[str]:
    """
    rows から保存対象日付を推定する。
    失敗した場合は None。
    """
    try:
        if rows is None:
            return None

        if isinstance(rows, pd.DataFrame):
            df = rows
        elif isinstance(rows, list):
            if not rows:
                return None
            df = pd.DataFrame(rows)
        else:
            df = pd.DataFrame(rows)

        if df.empty:
            return None

        for col in ("snapshot_time", "datetime", "inserted_at", "created_at"):
            if col not in df.columns:
                continue

            vals = pd.to_datetime(df[col], errors="coerce")
            vals = vals.dropna()
            if not vals.empty:
                return vals.iloc[0].strftime("%Y%m%d")

    except Exception:
        return None

    return None


def _try_call_engine_func(func: Any, db_path: Optional[Path]) -> Optional[Any]:
    """
    プロジェクト側 get_engine 系関数のシグネチャ違いを吸収して呼び出す。
    """
    if not callable(func):
        return None

    # 1. 引数なし
    try:
        eng = func()
        if eng is not None:
            return eng
    except TypeError:
        pass
    except Exception:
        logger.debug("[RANKING RAW] external engine func failed without args", exc_info=True)

    # 2. db_path positional
    if db_path is not None:
        try:
            eng = func(str(db_path))
            if eng is not None:
                return eng
        except TypeError:
            pass
        except Exception:
            logger.debug("[RANKING RAW] external engine func failed with path arg", exc_info=True)

    # 3. db_path keyword
    if db_path is not None:
        for key in ("db_path", "path", "database_path"):
            try:
                eng = func(**{key: str(db_path)})
                if eng is not None:
                    return eng
            except TypeError:
                continue
            except Exception:
                logger.debug(
                    "[RANKING RAW] external engine func failed with kw=%s",
                    key,
                    exc_info=True,
                )

    return None


def _try_external_engine_resolver(db_path: Optional[Path] = None) -> Optional[Any]:
    """
    既存プロジェクト側に engine resolver がある場合は使う。
    見つからない、または失敗した場合は None を返す。

    ここで失敗しても ranking_raw 保存全体は止めない。
    """
    candidates = [
        ("database.db_engine", "get_engine"),
        ("database.connection", "get_engine"),
        ("database.engine", "get_engine"),
        ("database.db_connection", "get_engine"),
        ("database.db_manager", "get_engine"),
        ("database.database_manager", "get_engine"),
        ("database.ranking_db", "get_ranking_engine"),
        ("database.ranking_db", "get_engine"),
        ("database.db_path", "get_engine"),
    ]

    for module_name, func_name in candidates:
        try:
            mod = __import__(module_name, fromlist=[func_name])
            func = getattr(mod, func_name, None)
            eng = _try_call_engine_func(func, db_path)
            if eng is not None:
                logger.info(
                    "[RANKING RAW] external engine resolver OK module=%s func=%s",
                    module_name,
                    func_name,
                )
                return eng
        except Exception:
            continue

    return None


def _make_sqlalchemy_engine_from_path(db_path: Path) -> Any:
    """
    SQLAlchemy engine を ranking DB パスから直接作る。
    """
    try:
        from sqlalchemy import create_engine
    except Exception as e:
        raise RuntimeError(
            "sqlalchemy is required for ranking_raw_1min engine fallback"
        ) from e

    db_path.parent.mkdir(parents=True, exist_ok=True)

    uri_path = db_path.as_posix()

    engine = create_engine(
        f"sqlite:///{uri_path}",
        connect_args={
            "timeout": 30,
            "check_same_thread": False,
        },
        pool_pre_ping=True,
        future=True,
    )

    return engine


def _apply_sqlite_pragmas_best_effort(engine: Any) -> None:
    """
    SQLite のロック軽減設定。
    失敗しても保存処理は止めない。
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA busy_timeout=30000"))
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))
    except Exception:
        logger.debug("[RANKING RAW] sqlite pragmas skipped/failed", exc_info=True)


def _get_engine(
    date_yyyymmdd: Optional[Any] = None,
    db_path: Optional[str | Path] = None,
) -> Any:
    """
    ranking_raw_1min 用 DB engine を取得する。

    重要:
      - resolver が見つからないだけで落とさない
      - rankingYYYYMMDD.db を自力解決して engine を作る
      - 日付/DBパスごとに cache する
    """
    resolved_db_path = Path(db_path) if db_path else _resolve_ranking_db_path(date_yyyymmdd)
    cache_key = str(resolved_db_path)

    cached = _ENGINE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # 1. 既存 resolver があれば使う
    engine = _try_external_engine_resolver(resolved_db_path)
    if engine is not None:
        _ENGINE_CACHE[cache_key] = engine
        _apply_sqlite_pragmas_best_effort(engine)
        return engine

    # 2. fallback: DB path から直接作る
    try:
        engine = _make_sqlalchemy_engine_from_path(resolved_db_path)
        _ENGINE_CACHE[cache_key] = engine
        _apply_sqlite_pragmas_best_effort(engine)

        logger.info(
            "[RANKING RAW] fallback sqlalchemy engine created db=%s",
            resolved_db_path,
        )
        return engine

    except Exception as e:
        logger.exception(
            "[RANKING RAW] fallback engine creation failed db=%s",
            resolved_db_path,
        )
        raise RuntimeError(
            f"ranking raw database engine could not be created: {resolved_db_path}"
        ) from e


# ------------------------------------------------------------
# safe converters
# ------------------------------------------------------------

def _is_null_like(v: Any) -> bool:
    try:
        if v is None:
            return True
        if v is pd.NaT:
            return True
        return bool(pd.isna(v))
    except Exception:
        return False


def _to_text(v: Any) -> Optional[str]:
    if _is_null_like(v):
        return None
    try:
        s = str(v).strip()
        return s if s else None
    except Exception:
        return None


def _to_int(v: Any) -> Optional[int]:
    if _is_null_like(v):
        return None
    try:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
        return int(float(v))
    except Exception:
        return None


def _to_float(v: Any) -> Optional[float]:
    if _is_null_like(v):
        return None
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _to_sqlite_datetime_text(v: Any) -> Optional[str]:
    if _is_null_like(v):
        return None

    try:
        if isinstance(v, pd.Timestamp):
            if pd.isna(v):
                return None
            if v.tzinfo is not None:
                try:
                    v = v.tz_convert(None)
                except Exception:
                    try:
                        v = v.tz_localize(None)
                    except Exception:
                        pass
            return v.to_pydatetime().strftime("%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        pass

    try:
        if isinstance(v, datetime):
            if v.tzinfo is not None:
                try:
                    v = v.replace(tzinfo=None)
                except Exception:
                    pass
            return v.strftime("%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        pass

    try:
        if isinstance(v, date):
            return datetime(v.year, v.month, v.day).strftime("%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        pass

    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        if isinstance(ts, pd.Timestamp):
            if ts.tzinfo is not None:
                try:
                    ts = ts.tz_convert(None)
                except Exception:
                    try:
                        ts = ts.tz_localize(None)
                    except Exception:
                        pass
            return ts.to_pydatetime().strftime("%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        pass

    return _to_text(v)


# ------------------------------------------------------------
# normalize
# ------------------------------------------------------------

def _ensure_dataframe(rows: Any) -> pd.DataFrame:
    if rows is None:
        return pd.DataFrame(columns=_EXPECTED_COLUMNS)

    if isinstance(rows, pd.DataFrame):
        df = rows.copy()
    elif isinstance(rows, list):
        if not rows:
            return pd.DataFrame(columns=_EXPECTED_COLUMNS)
        df = pd.DataFrame(rows)
    else:
        try:
            df = pd.DataFrame(rows)
        except Exception:
            return pd.DataFrame(columns=_EXPECTED_COLUMNS)

    if df.empty:
        return pd.DataFrame(columns=_EXPECTED_COLUMNS)

    # よくある別名吸収
    alias_map = {
        "datetime": "snapshot_time",
        "time": "snapshot_time",
        "name": "symbolname",
        "symbol_name": "symbolname",
        "ranking_type": "rank_type",
        "type": "rank_type",
        "rank": "rank_position",
        "current": "current_price",
        "price": "current_price",
        "volume": "trading_volume",
        "売買高": "trading_volume",
        "売買代金": "trading_value",
        "tick": "tick_count",
        "ticks": "tick_count",
    }

    for src, dst in alias_map.items():
        if dst not in df.columns and src in df.columns:
            df[dst] = df[src]

    for c in _EXPECTED_COLUMNS:
        if c not in df.columns:
            df[c] = None

    return df[_EXPECTED_COLUMNS].copy()


def _normalize_ranking_raw_rows(rows: Any) -> List[Dict[str, Any]]:
    df = _ensure_dataframe(rows)
    if df.empty:
        return []

    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce")
    df["inserted_at"] = pd.to_datetime(df["inserted_at"], errors="coerce")
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")

    now_ts = pd.Timestamp.now()

    df["inserted_at"] = df["inserted_at"].fillna(now_ts)
    df["created_at"] = df["created_at"].fillna(now_ts)

    df["symbol"] = df["symbol"].map(_to_text)
    df = df[df["symbol"].notna()].copy()
    df = df[df["snapshot_time"].notna()].copy()

    if df.empty:
        return []

    df["symbolname"] = df["symbolname"].map(_to_text)
    df["rank_type"] = df["rank_type"].map(_to_text)
    df["rank_type_id"] = df["rank_type_id"].map(_to_int)
    df["market"] = df["market"].map(_to_text)
    df["rank_position"] = df["rank_position"].map(_to_int)
    df["current_price"] = df["current_price"].map(_to_float)
    df["change_percentage"] = df["change_percentage"].map(_to_float)
    df["change_ratio"] = df["change_ratio"].map(_to_float)
    df["trading_volume"] = df["trading_volume"].map(_to_float)
    df["trading_value"] = df["trading_value"].map(_to_float)
    df["turnover"] = df["turnover"].map(_to_float)
    df["tick_count"] = df["tick_count"].map(_to_int)
    df["volume_speed"] = df["volume_speed"].map(_to_float)
    df["price_delta_1m"] = df["price_delta_1m"].map(_to_float)
    df["volume_delta_1m"] = df["volume_delta_1m"].map(_to_float)
    df["minute_of_day"] = df["minute_of_day"].map(_to_int)
    df["source"] = df["source"].map(_to_text)

    now_txt = _to_sqlite_datetime_text(now_ts)

    out: List[Dict[str, Any]] = []
    dropped = 0

    for row in df.to_dict(orient="records"):
        rec = {
            "symbol": _to_text(row.get("symbol")),
            "snapshot_time": _to_sqlite_datetime_text(row.get("snapshot_time")),
            "symbolname": _to_text(row.get("symbolname")),
            "rank_type": _to_text(row.get("rank_type")),
            "rank_type_id": _to_int(row.get("rank_type_id")),
            "market": _to_text(row.get("market")),
            "rank_position": _to_int(row.get("rank_position")),
            "current_price": _to_float(row.get("current_price")),
            "change_percentage": _to_float(row.get("change_percentage")),
            "change_ratio": _to_float(row.get("change_ratio")),
            "trading_volume": _to_float(row.get("trading_volume")),
            "trading_value": _to_float(row.get("trading_value")),
            "turnover": _to_float(row.get("turnover")),
            "tick_count": _to_int(row.get("tick_count")),
            "volume_speed": _to_float(row.get("volume_speed")),
            "price_delta_1m": _to_float(row.get("price_delta_1m")),
            "volume_delta_1m": _to_float(row.get("volume_delta_1m")),
            "minute_of_day": _to_int(row.get("minute_of_day")),
            "source": _to_text(row.get("source")) or "KABU_STATION",
            "inserted_at": _to_sqlite_datetime_text(row.get("inserted_at")) or now_txt,
            "created_at": _to_sqlite_datetime_text(row.get("created_at")) or now_txt,
        }

        if (
            rec["symbol"]
            and rec["snapshot_time"]
            and rec["rank_type_id"] is not None
            and rec["market"]
        ):
            out.append(rec)
        else:
            dropped += 1

    if dropped:
        logger.warning("[RANKING RAW] dropped unsafe rows=%s", dropped)

    return out


def _dedupe_safe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []

    for r in rows:
        key = (
            r.get("symbol"),
            r.get("snapshot_time"),
            r.get("rank_type_id"),
            r.get("market"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)

    removed = len(rows) - len(out)
    if removed:
        logger.warning("[RANKING RAW] duplicate rows removed in batch=%s", removed)

    return out


# ------------------------------------------------------------
# schema / migration helpers
# ------------------------------------------------------------

def _quote_ident(name: str) -> str:
    safe = str(name).replace('"', '""')
    return f'"{safe}"'


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table_name},
    ).fetchone()
    return row is not None


def _schema_cache_key_from_conn(conn) -> str:
    """
    SQLite DB の実ファイルパスを schema cache key にする。
    取得できない場合だけ connection id を使う。
    """
    try:
        row = conn.execute(text("PRAGMA database_list")).fetchone()
        if row is not None:
            try:
                db_file = row[2]
            except Exception:
                db_file = None

            if db_file:
                return str(db_file)
    except Exception:
        pass

    return f"conn:{id(conn)}"


def _get_index_columns(conn, index_name: str) -> List[str]:
    rows = conn.execute(text(f"PRAGMA index_info({_quote_ident(index_name)})")).fetchall()
    cols: List[str] = []

    for r in rows:
        try:
            cols.append(str(r[2]))
        except Exception:
            pass

    return cols


def _get_table_columns(conn, table_name: str) -> List[str]:
    rows = conn.execute(text(f"PRAGMA table_info({_quote_ident(table_name)})")).fetchall()
    cols: List[str] = []

    for r in rows:
        try:
            cols.append(str(r[1]))
        except Exception:
            pass

    return cols


def _detect_legacy_unique_symbol_snapshot(conn) -> bool:
    rows = conn.execute(text(f"PRAGMA index_list({_quote_ident(TABLE_NAME)})")).fetchall()

    for r in rows:
        try:
            name = str(r[1])
            is_unique = int(r[2]) == 1
        except Exception:
            continue

        if not is_unique:
            continue

        cols = _get_index_columns(conn, name)

        if cols == ["symbol", "snapshot_time"]:
            logger.warning(
                "[RANKING RAW][SCHEMA] legacy unique detected: %s cols=%s",
                name,
                cols,
            )
            return True

    return False


def _detect_wrong_unique_index(conn) -> bool:
    """
    ranking_raw_1min に UNIQUE(symbol, snapshot_time, rank_type_id, market)
    がない場合は migration 対象にする。
    """
    if not _table_exists(conn, TABLE_NAME):
        return False

    expected = ["symbol", "snapshot_time", "rank_type_id", "market"]

    rows = conn.execute(text(f"PRAGMA index_list({_quote_ident(TABLE_NAME)})")).fetchall()

    for r in rows:
        try:
            name = str(r[1])
            is_unique = int(r[2]) == 1
        except Exception:
            continue

        if not is_unique:
            continue

        cols = _get_index_columns(conn, name)
        if cols == expected:
            return False

    logger.warning("[RANKING RAW][SCHEMA] expected unique index not found")
    return True


def _requires_table_migration(conn) -> bool:
    if not _table_exists(conn, TABLE_NAME):
        return False

    cols = set(_get_table_columns(conn, TABLE_NAME))

    required = {
        "symbol",
        "snapshot_time",
        "symbolname",
        "rank_type",
        "rank_type_id",
        "market",
        "rank_position",
        "current_price",
        "change_percentage",
        "change_ratio",
        "trading_volume",
        "trading_value",
        "turnover",
        "tick_count",
        "volume_speed",
        "price_delta_1m",
        "volume_delta_1m",
        "minute_of_day",
        "source",
        "inserted_at",
        "created_at",
    }

    missing = sorted(list(required - cols))
    if missing:
        logger.warning("[RANKING RAW][SCHEMA] missing columns detected: %s", missing)
        return True

    if _detect_legacy_unique_symbol_snapshot(conn):
        return True

    if _detect_wrong_unique_index(conn):
        return True

    return False


def _select_expr_for_migration(col: str, existing_cols: List[str]) -> str:
    existing = set(existing_cols)

    if col in existing:
        return _quote_ident(col)

    # 旧 created_at を inserted_at に流用
    if col == "inserted_at" and "created_at" in existing:
        return "created_at AS inserted_at"

    # 旧 rank / rank_position 揺れ
    if col == "rank_position" and "rank" in existing:
        return "rank AS rank_position"

    # 旧 datetime / snapshot_time 揺れ
    if col == "snapshot_time" and "datetime" in existing:
        return "datetime AS snapshot_time"

    # 旧 name / symbolname 揺れ
    if col == "symbolname" and "name" in existing:
        return "name AS symbolname"

    # 旧 ranking_type / rank_type 揺れ
    if col == "rank_type" and "ranking_type" in existing:
        return "ranking_type AS rank_type"

    # 旧 price / current_price 揺れ
    if col == "current_price" and "price" in existing:
        return "price AS current_price"

    # 旧 volume / trading_volume 揺れ
    if col == "trading_volume" and "volume" in existing:
        return "volume AS trading_volume"

    return f"NULL AS {_quote_ident(col)}"


def _build_copy_distinct_to_tmp_sql(existing_cols: List[str]) -> str:
    insert_cols = [
        "symbol",
        "snapshot_time",
        "symbolname",
        "rank_type",
        "rank_type_id",
        "market",
        "rank_position",
        "current_price",
        "change_percentage",
        "change_ratio",
        "trading_volume",
        "trading_value",
        "turnover",
        "tick_count",
        "volume_speed",
        "price_delta_1m",
        "volume_delta_1m",
        "minute_of_day",
        "source",
        "inserted_at",
        "created_at",
    ]

    select_exprs = [
        _select_expr_for_migration(col, existing_cols)
        for col in insert_cols
    ]

    where_parts = []
    existing = set(existing_cols)

    if "symbol" in existing:
        where_parts.append("symbol IS NOT NULL")

    if "snapshot_time" in existing:
        where_parts.append("snapshot_time IS NOT NULL")
    elif "datetime" in existing:
        where_parts.append("datetime IS NOT NULL")

    if "rank_type_id" in existing:
        where_parts.append("rank_type_id IS NOT NULL")

    if "market" in existing:
        where_parts.append("market IS NOT NULL")

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + "\n  AND ".join(where_parts)

    return f"""
INSERT OR IGNORE INTO {TMP_TABLE_NAME} (
    {", ".join(insert_cols)}
)
SELECT
    {", ".join(select_exprs)}
FROM {TABLE_NAME}
{where_sql}
"""


def _drop_old_indexes_best_effort(conn) -> None:
    """
    旧 index が残って rename 後に邪魔するケースを避ける。
    """
    try:
        rows = conn.execute(text(f"PRAGMA index_list({_quote_ident(TABLE_NAME)})")).fetchall()
        for r in rows:
            try:
                name = str(r[1])
            except Exception:
                continue

            if name.startswith("sqlite_autoindex"):
                continue

            try:
                conn.execute(text(f"DROP INDEX IF EXISTS {_quote_ident(name)}"))
                logger.warning("[RANKING RAW][SCHEMA] dropped old index=%s", name)
            except Exception:
                logger.debug(
                    "[RANKING RAW][SCHEMA] drop old index failed index=%s",
                    name,
                    exc_info=True,
                )
    except Exception:
        logger.debug("[RANKING RAW][SCHEMA] index cleanup skipped", exc_info=True)


def _migrate_legacy_table(conn) -> None:
    logger.warning("[RANKING RAW][SCHEMA] start table migration")

    existing_cols = _get_table_columns(conn, TABLE_NAME)
    copy_sql = _build_copy_distinct_to_tmp_sql(existing_cols)

    conn.execute(text(f"DROP TABLE IF EXISTS {TMP_TABLE_NAME}"))
    conn.execute(text(CREATE_TMP_TABLE_SQL))
    conn.execute(text(CREATE_TMP_UNIQUE_INDEX_SQL))
    conn.execute(text(copy_sql))

    _drop_old_indexes_best_effort(conn)

    conn.execute(text(f"DROP TABLE {TABLE_NAME}"))
    conn.execute(text(f"ALTER TABLE {TMP_TABLE_NAME} RENAME TO {TABLE_NAME}"))
    conn.execute(text(CREATE_UNIQUE_INDEX_SQL))

    logger.warning("[RANKING RAW][SCHEMA] table migration completed")


def ensure_ranking_raw_1min_table(engine: Optional[Any] = None) -> None:
    """
    ranking_raw_1min の schema / unique index を保証する。

    改善:
      - 元コードでは保存ごとに毎回 CREATE INDEX / migration 判定が走っていた。
      - DBファイル単位で schema ensure 済みなら即 return する。
      - これによりランキング毎分保存の負荷を下げる。
    """
    engine = engine or _get_engine()

    with engine.begin() as conn:
        conn.execute(text("PRAGMA busy_timeout=30000"))
        key = _schema_cache_key_from_conn(conn)

        if key in _SCHEMA_ENSURED_DB_KEYS:
            return

        conn.execute(text(CREATE_TABLE_SQL))

        if _requires_table_migration(conn):
            _migrate_legacy_table(conn)

        conn.execute(text(CREATE_UNIQUE_INDEX_SQL))

        _SCHEMA_ENSURED_DB_KEYS.add(key)

    logger.info("✅ ranking_raw_1min unique index ensured cached_key=%s", key)


# ------------------------------------------------------------
# public
# ------------------------------------------------------------

def insert_ranking_raw_1min(*args, **kwargs) -> int:
    """
    ranking_raw_1min へ保存する。

    互換ラッパ:
      insert_ranking_raw_1min(rows)
      insert_ranking_raw_1min(rows, engine=engine)
      insert_ranking_raw_1min(engine, rows=rows)
      insert_ranking_raw_1min(rows=rows)
      insert_ranking_raw_1min(engine=engine, rows=rows)

    追加対応:
      insert_ranking_raw_1min(rows, db_path="...")
      insert_ranking_raw_1min(rows, date_yyyymmdd="20260430")

    Returns
    -------
    int
      新規INSERTされた行数。
      UNIQUE重複の場合は 0 になるが、これは失敗ではない。
    """
    engine = kwargs.pop("engine", None)
    rows = kwargs.pop("rows", None)
    db_path = kwargs.pop("db_path", None)
    date_yyyymmdd = kwargs.pop("date_yyyymmdd", None)

    if kwargs:
        logger.warning("[RANKING RAW] unexpected kwargs ignored: %s", list(kwargs.keys()))

    if len(args) == 1:
        a0 = args[0]
        if rows is not None and engine is None:
            engine = a0
        elif rows is None:
            rows = a0
        else:
            logger.warning("[RANKING RAW] redundant positional arg ignored: %r", type(a0))

    elif len(args) >= 2:
        if engine is None:
            engine = args[0]
        if rows is None:
            rows = args[1]
        if len(args) > 2:
            logger.warning(
                "[RANKING RAW] extra positional args ignored: count=%s",
                len(args) - 2,
            )

    if date_yyyymmdd is None:
        date_yyyymmdd = _infer_date_yyyymmdd_from_rows(rows)

    if engine is None:
        engine = _get_engine(date_yyyymmdd=date_yyyymmdd, db_path=db_path)

    safe_rows = _normalize_ranking_raw_rows(rows)
    safe_rows = _dedupe_safe_rows(safe_rows)

    if not safe_rows:
        logger.warning("⚠ ranking_raw_1min insert skipped: no safe rows")
        return 0

    try:
        snapshot_vals = [r["snapshot_time"] for r in safe_rows if r.get("snapshot_time")]
        symbol_count = len({r["symbol"] for r in safe_rows if r.get("symbol")})
        logger.info(
            "[RANKING RAW SAVE] normalized rows=%s symbols=%s snapshot_min=%s snapshot_max=%s",
            len(safe_rows),
            symbol_count,
            min(snapshot_vals) if snapshot_vals else None,
            max(snapshot_vals) if snapshot_vals else None,
        )
    except Exception:
        logger.info("[RANKING RAW SAVE] normalized rows=%s", len(safe_rows))

    ensure_ranking_raw_1min_table(engine)

    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA busy_timeout=30000"))
            result = conn.execute(text(INSERT_SQL), safe_rows)
            rowcount = int(getattr(result, "rowcount", 0) or 0)

        logger.info(
            "✅ ranking_raw_1min inserted rows=%s input=%s",
            rowcount,
            len(safe_rows),
        )
        return rowcount

    except Exception:
        logger.exception("❌ ranking_raw_1min insert failed")

    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA busy_timeout=30000"))
            result = conn.execute(text(INSERT_OR_IGNORE_SQL), safe_rows)
            rowcount = int(getattr(result, "rowcount", 0) or 0)

        logger.info(
            "✅ ranking_raw_1min fallback inserted rows=%s input=%s",
            rowcount,
            len(safe_rows),
        )
        return rowcount

    except Exception:
        logger.exception("❌ ranking_raw_1min fallback insert failed")
        return 0


def save_ranking_raw_1min(*args, **kwargs) -> int:
    """
    別名互換。
    """
    return insert_ranking_raw_1min(*args, **kwargs)


def ensure_table(engine: Optional[Any] = None) -> None:
    """
    別名互換。
    """
    ensure_ranking_raw_1min_table(engine)


__all__ = [
    "TABLE_NAME",
    "insert_ranking_raw_1min",
    "save_ranking_raw_1min",
    "ensure_ranking_raw_1min_table",
    "ensure_table",
]