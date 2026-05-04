# ============================================================
# File   : trading/ranking/ranking_ma_builder.py
# Version: Ver1.2-PRODUCTION-HARDENED-RANKING-MA-BUILDER
# ------------------------------------------------------------
# ✔ ranking_ma_1min schema ensure
# ✔ datetime 列の後付け追加対応
# ✔ duplicate cleanup before unique index
# ✔ SQLite-safe upsert
# ✔ pandas.Timestamp safe conversion
# ✔ 旧呼び出し互換 now= 対応
# ✔ engine resolver 強化
# ✔ snapshot_time -> datetime 補完
# ✔ slope / rsi / macd columns prepared
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)


# ============================================================
# engine resolver
# ============================================================

def _try_get_attr(obj: Any, names: list[str]) -> Any:
    for name in names:
        try:
            v = getattr(obj, name, None)
            if v is not None:
                return v
        except Exception:
            pass
    return None


def _build_sqlite_engine_from_path(db_path: str):
    if not db_path:
        return None
    try:
        p = str(db_path).strip()
        if not p:
            return None
        return create_engine(
            f"sqlite:///{p}",
            future=True,
            pool_pre_ping=True,
            connect_args={"timeout": 60},
        )
    except Exception:
        logger.exception("[RANKING_MA] create_engine failed from path=%s", db_path)
        return None


def _resolve_db_path_from_project() -> Optional[str]:
    candidates: list[Any] = []

    try:
        from global_state import global_data  # type: ignore
        candidates.extend([
            _try_get_attr(global_data, ["ranking_db_path"]),
            _try_get_attr(global_data, ["ranking_path"]),
            _try_get_attr(global_data, ["db_path_ranking"]),
            _try_get_attr(global_data, ["ranking_runtime_db"]),
        ])
    except Exception:
        pass

    module_candidates = [
        "config.paths",
        "config.settings",
        "config.config",
        "settings",
        "database.paths",
        "database.config",
    ]
    for mod_name in module_candidates:
        try:
            mod = __import__(mod_name, fromlist=["dummy"])
            candidates.extend([
                _try_get_attr(mod, ["RANKING_DB_PATH"]),
                _try_get_attr(mod, ["RANKING_RUNTIME_DB_PATH"]),
                _try_get_attr(mod, ["DB_PATH_RANKING"]),
            ])
        except Exception:
            pass

    try:
        now = datetime.now()
        ymd = now.strftime("%Y%m%d")
        candidates.extend([
            rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking{ymd}.db",
            rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking_{ymd}.db",
        ])
    except Exception:
        pass

    for c in candidates:
        try:
            if c and isinstance(c, (str, Path)):
                s = str(c)
                if s.strip():
                    return s
        except Exception:
            pass

    return None


def _get_engine():
    import_candidates = [
        ("database.db_engine", "get_engine"),
        ("database.connection", "get_engine"),
        ("database.engine", "get_engine"),
        ("database.database", "get_engine"),
        ("database.db", "get_engine"),
        ("core.db", "get_engine"),
        ("core.database", "get_engine"),
        ("core.db_engine", "get_engine"),
        ("trading.database", "get_engine"),
    ]

    for mod_name, fn_name in import_candidates:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                eng = fn()
                if eng is not None:
                    logger.info("[RANKING_MA] engine resolved via %s.%s", mod_name, fn_name)
                    return eng
        except Exception:
            pass

    try:
        from global_state import global_data  # type: ignore

        eng = _try_get_attr(global_data, [
            "ranking_engine",
            "db_engine_ranking",
            "engine_ranking",
            "ranking_db_engine",
        ])
        if eng is not None:
            logger.info("[RANKING_MA] engine resolved via global_data engine attr")
            return eng
    except Exception:
        pass

    db_path = _resolve_db_path_from_project()
    if db_path:
        eng = _build_sqlite_engine_from_path(db_path)
        if eng is not None:
            logger.info("[RANKING_MA] engine resolved via sqlite path=%s", db_path)
            return eng

    raise RuntimeError("database engine resolver not found")


# ============================================================
# safe helpers
# ============================================================

def _is_null(v: Any) -> bool:
    try:
        if v is None:
            return True
        if v is pd.NaT:
            return True
        return bool(pd.isna(v))
    except Exception:
        return False


def _to_text(v: Any) -> Optional[str]:
    if _is_null(v):
        return None
    try:
        s = str(v).strip()
        return s if s else None
    except Exception:
        return None


def _to_float(v: Any) -> Optional[float]:
    if _is_null(v):
        return None
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _to_int(v: Any) -> Optional[int]:
    if _is_null(v):
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _to_dt_text(v: Any) -> Optional[str]:
    if _is_null(v):
        return None
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
            return ts.to_pydatetime().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return _to_text(v)


# ============================================================
# schema
# ============================================================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ranking_ma_1min (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    datetime TEXT NOT NULL,
    symbolname TEXT,
    close REAL,
    score REAL,
    slope REAL,
    rsi REAL,
    macd REAL,
    macd_signal REAL,
    macd_hist REAL,
    best_rank INTEGER,
    rank_type TEXT,
    source TEXT,
    created_at TEXT,
    updated_at TEXT
)
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_ranking_ma_1min_datetime
ON ranking_ma_1min(datetime)
"""

UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_ranking_ma_1min_symbol_datetime
ON ranking_ma_1min(symbol, datetime)
"""

ADD_COLUMNS = {
    "datetime": "ALTER TABLE ranking_ma_1min ADD COLUMN datetime TEXT",
    "symbolname": "ALTER TABLE ranking_ma_1min ADD COLUMN symbolname TEXT",
    "close": "ALTER TABLE ranking_ma_1min ADD COLUMN close REAL",
    "score": "ALTER TABLE ranking_ma_1min ADD COLUMN score REAL",
    "slope": "ALTER TABLE ranking_ma_1min ADD COLUMN slope REAL",
    "rsi": "ALTER TABLE ranking_ma_1min ADD COLUMN rsi REAL",
    "macd": "ALTER TABLE ranking_ma_1min ADD COLUMN macd REAL",
    "macd_signal": "ALTER TABLE ranking_ma_1min ADD COLUMN macd_signal REAL",
    "macd_hist": "ALTER TABLE ranking_ma_1min ADD COLUMN macd_hist REAL",
    "best_rank": "ALTER TABLE ranking_ma_1min ADD COLUMN best_rank INTEGER",
    "rank_type": "ALTER TABLE ranking_ma_1min ADD COLUMN rank_type TEXT",
    "source": "ALTER TABLE ranking_ma_1min ADD COLUMN source TEXT",
    "created_at": "ALTER TABLE ranking_ma_1min ADD COLUMN created_at TEXT",
    "updated_at": "ALTER TABLE ranking_ma_1min ADD COLUMN updated_at TEXT",
}

UPSERT_SQL = """
INSERT INTO ranking_ma_1min (
    symbol,
    datetime,
    symbolname,
    close,
    score,
    slope,
    rsi,
    macd,
    macd_signal,
    macd_hist,
    best_rank,
    rank_type,
    source,
    created_at,
    updated_at
)
VALUES (
    :symbol,
    :datetime,
    :symbolname,
    :close,
    :score,
    :slope,
    :rsi,
    :macd,
    :macd_signal,
    :macd_hist,
    :best_rank,
    :rank_type,
    :source,
    :created_at,
    :updated_at
)
ON CONFLICT(symbol, datetime) DO UPDATE SET
    symbolname   = excluded.symbolname,
    close        = excluded.close,
    score        = excluded.score,
    slope        = excluded.slope,
    rsi          = excluded.rsi,
    macd         = excluded.macd,
    macd_signal  = excluded.macd_signal,
    macd_hist    = excluded.macd_hist,
    best_rank    = excluded.best_rank,
    rank_type    = excluded.rank_type,
    source       = excluded.source,
    updated_at   = excluded.updated_at
"""


def _get_existing_columns(conn) -> set[str]:
    rows = conn.execute(text("PRAGMA table_info(ranking_ma_1min)")).fetchall()
    cols = set()
    for r in rows:
        try:
            cols.add(str(r[1]))
        except Exception:
            pass
    return cols


def _fill_datetime_from_snapshot_time_if_possible(conn, cols: set[str]) -> None:
    if "datetime" not in cols:
        return
    if "snapshot_time" not in cols:
        return
    try:
        conn.execute(text("""
            UPDATE ranking_ma_1min
               SET datetime = snapshot_time
             WHERE (datetime IS NULL OR TRIM(datetime) = '')
               AND snapshot_time IS NOT NULL
        """))
        logger.info("[RANKING][SCHEMA] backfilled datetime from snapshot_time")
    except Exception:
        logger.exception("[RANKING][SCHEMA] datetime backfill failed")


def _drop_duplicate_symbol_datetime(conn) -> int:
    dup_count_sql = """
    SELECT COUNT(*) FROM (
        SELECT symbol, datetime, COUNT(*) c
        FROM ranking_ma_1min
        WHERE datetime IS NOT NULL
        GROUP BY symbol, datetime
        HAVING COUNT(*) > 1
    )
    """
    try:
        dup_groups = conn.execute(text(dup_count_sql)).scalar() or 0
    except Exception:
        dup_groups = 0

    if not dup_groups:
        return 0

    delete_sql = """
    DELETE FROM ranking_ma_1min
    WHERE id NOT IN (
        SELECT MAX(id)
        FROM ranking_ma_1min
        WHERE datetime IS NOT NULL
        GROUP BY symbol, datetime
    )
    """
    result = conn.execute(text(delete_sql))
    deleted = int(getattr(result, "rowcount", 0) or 0)
    logger.warning(
        "[RANKING_MA] duplicate cleanup done dup_groups=%s deleted_rows=%s",
        dup_groups,
        deleted,
    )
    return deleted


def _ensure_ranking_ma_1min_schema(conn) -> None:
    conn.execute(text(CREATE_TABLE_SQL))

    cols = _get_existing_columns(conn)
    for col, sql in ADD_COLUMNS.items():
        if col not in cols:
            try:
                conn.execute(text(sql))
                logger.info("[RANKING][SCHEMA] added column '%s'", col)
            except Exception:
                logger.exception("[RANKING][SCHEMA] add column failed: %s", col)

    cols = _get_existing_columns(conn)
    _fill_datetime_from_snapshot_time_if_possible(conn, cols)
    _drop_duplicate_symbol_datetime(conn)

    cols = _get_existing_columns(conn)
    if "datetime" not in cols:
        raise RuntimeError("ranking_ma_1min schema ensure failed: datetime column missing")

    conn.execute(text(INDEX_SQL))
    conn.execute(text(UNIQUE_INDEX_SQL))


# ============================================================
# normalize
# ============================================================

def _ensure_df(df: Any) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    if isinstance(df, pd.DataFrame):
        out = df.copy()
    else:
        try:
            out = pd.DataFrame(df).copy()
        except Exception:
            return pd.DataFrame()
    if out.empty:
        return pd.DataFrame()
    return out


def _normalize_input(df: pd.DataFrame) -> List[Dict[str, Any]]:
    out = _ensure_df(df)
    if out.empty:
        return []

    if "symbol" not in out.columns:
        return []

    if "datetime" not in out.columns:
        if "snapshot_time" in out.columns:
            out["datetime"] = out["snapshot_time"]
        else:
            return []

    if "created_at" not in out.columns:
        out["created_at"] = pd.Timestamp.now()

    if "updated_at" not in out.columns:
        out["updated_at"] = pd.Timestamp.now()

    defaults = {
        "symbolname": None,
        "close": None,
        "score": None,
        "slope": None,
        "rsi": None,
        "macd": None,
        "macd_signal": None,
        "macd_hist": None,
        "best_rank": None,
        "rank_type": None,
        "source": "RANKING",
    }
    for k, v in defaults.items():
        if k not in out.columns:
            out[k] = v

    safe_rows: List[Dict[str, Any]] = []
    for row in out.to_dict(orient="records"):
        rec = {
            "symbol": _to_text(row.get("symbol")),
            "datetime": _to_dt_text(row.get("datetime")),
            "symbolname": _to_text(row.get("symbolname")),
            "close": _to_float(row.get("close")),
            "score": _to_float(row.get("score")),
            "slope": _to_float(row.get("slope")),
            "rsi": _to_float(row.get("rsi")),
            "macd": _to_float(row.get("macd")),
            "macd_signal": _to_float(row.get("macd_signal")),
            "macd_hist": _to_float(row.get("macd_hist")),
            "best_rank": _to_int(row.get("best_rank")),
            "rank_type": _to_text(row.get("rank_type")),
            "source": _to_text(row.get("source")) or "RANKING",
            "created_at": _to_dt_text(row.get("created_at")) or _to_dt_text(datetime.now()),
            "updated_at": _to_dt_text(row.get("updated_at")) or _to_dt_text(datetime.now()),
        }
        if rec["symbol"] and rec["datetime"]:
            safe_rows.append(rec)

    return safe_rows


# ============================================================
# public
# ============================================================

def build_ranking_ma_1min(
    df: pd.DataFrame = None,
    engine=None,
    now=None,
    **kwargs,
) -> int:
    if df is None:
        df = kwargs.pop("rows", None)

    if df is None:
        logger.warning("[RANKING_MA] build skipped: input df is None")
        return 0

    if engine is None:
        engine = _get_engine()

    try:
        if isinstance(df, pd.DataFrame):
            work = df.copy()
        else:
            work = pd.DataFrame(df).copy()
    except Exception:
        logger.exception("[RANKING_MA] dataframe conversion failed")
        return 0

    if work.empty:
        logger.warning("[RANKING_MA] build skipped: empty rows")
        return 0

    if "datetime" not in work.columns:
        if "snapshot_time" in work.columns:
            work["datetime"] = work["snapshot_time"]
        elif now is not None:
            work["datetime"] = now

    if "updated_at" not in work.columns:
        work["updated_at"] = now if now is not None else pd.Timestamp.now()

    if "created_at" not in work.columns:
        work["created_at"] = now if now is not None else pd.Timestamp.now()

    safe_rows = _normalize_input(work)
    if not safe_rows:
        logger.warning("[RANKING_MA] build skipped: no normalized rows")
        return 0

    try:
        with engine.begin() as conn:
            _ensure_ranking_ma_1min_schema(conn)
            result = conn.execute(text(UPSERT_SQL), safe_rows)
            rowcount = int(getattr(result, "rowcount", 0) or 0)

        logger.info(
            "✅ ranking_ma_1min upsert done rows=%s input=%s",
            rowcount,
            len(safe_rows),
        )
        return rowcount

    except Exception:
        logger.exception("❌ ranking_ma_1min upsert failed")
        return 0