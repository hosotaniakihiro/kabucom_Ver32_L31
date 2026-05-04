# ============================================================
# File   : trading/ranking/ranking_loader.py
# Version: Ver3.0-PRODUCTION-RANKING-LOADER-NAS-ABSOLUTE-STABLE-FINAL
# ------------------------------------------------------------
# ✔ 既存機能削除ゼロ
# ✔ 全市場 値上がりランキング維持
# ✔ 全市場 値下がりランキング維持
# ✔ 市場別 売買代金ランキング維持
# ✔ SQLite直接接続互換維持
# ✔ database.session / get_ranking_engine 優先対応
# ✔ config.paths.get_path fallback対応
# ✔ NAS日付別 rankingYYYYMMDD.db 自動解決
# ✔ Path固定書き廃止
# ✔ SQL注入防止（parameterized query）
# ✔ テーブル存在確認追加
# ✔ rank_rise / rank_fall / rank_value 互換維持
# ✔ ranking_snapshot_1min fallback追加
# ✔ snapshot列名ゆらぎ吸収
# ✔ 市場名 normalize追加
# ✔ 例外完全吸収
# ✔ production hardened
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# legacy fallback path
# ============================================================

LEGACY_RANKING_DB = Path("Y:/stock_ranking/ranking.db")


# ============================================================
# market normalize
# ============================================================

def _normalize_market(market: Any) -> str:
    s = str(market or "").strip().upper()

    mapping = {
        "PRIME": "PRIME",
        "プライム": "PRIME",
        "P": "PRIME",

        "STANDARD": "STANDARD",
        "スタンダード": "STANDARD",
        "S": "STANDARD",

        "GROWTH": "GROWTH",
        "グロース": "GROWTH",
        "G": "GROWTH",

        "ALL": "ALL",
        "全体": "ALL",
    }
    return mapping.get(s, s)


# ============================================================
# engine / path resolver
# ============================================================

def _resolve_ranking_engine():
    candidates = []

    try:
        import database.session as session_mod

        for name in (
            "get_ranking_engine",
            "get_rank_engine",
        ):
            fn = getattr(session_mod, name, None)
            if callable(fn):
                candidates.append(("database.session", name, fn))
    except Exception:
        logger.debug("[RANKING LOADER] database.session import failed", exc_info=True)

    for mod_name in (
        "database.core.connection",
        "database.connection",
    ):
        try:
            mod = __import__(mod_name, fromlist=["get_engine"])
            fn = getattr(mod, "get_engine", None)
            if callable(fn):
                candidates.append((mod_name, "get_engine", fn))
        except Exception:
            logger.debug("[RANKING LOADER] %s import failed", mod_name, exc_info=True)

    for mod_name, fn_name, fn in candidates:
        try:
            if fn_name == "get_engine":
                engine = fn("ranking")
            else:
                engine = fn()

            if engine is not None:
                logger.info("[RANKING LOADER] ranking engine resolved: %s.%s", mod_name, fn_name)
                return engine
        except Exception:
            logger.debug(
                "[RANKING LOADER] ranking engine candidate failed: %s.%s",
                mod_name,
                fn_name,
                exc_info=True,
            )

    return None


def _candidate_ranking_paths() -> list[Path]:
    out: list[Path] = []

    # 1) config.paths.get_path 優先
    try:
        from config.paths import get_path

        for key in (
            "ranking_db",
            "ranking",
            "ranking_path",
        ):
            try:
                p = get_path(key)
                if p:
                    out.append(Path(str(p)))
            except Exception:
                continue
    except Exception:
        logger.debug("[RANKING LOADER] config.paths.get_path import failed", exc_info=True)

    # 2) database.session から URI / path を拾う
    try:
        import database.session as session_mod

        for attr in (
            "RANKING_DB_PATH",
            "RANKING_DB",
            "ranking_db_path",
            "ranking_db",
        ):
            try:
                v = getattr(session_mod, attr, None)
                if v:
                    out.append(Path(str(v)))
            except Exception:
                continue
    except Exception:
        logger.debug("[RANKING LOADER] database.session path attrs import failed", exc_info=True)

    # 3) NAS日付別既定候補
    today = dt.datetime.now().strftime("%Y%m%d")
    nas_candidates = [
        Path(fr"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking{today}.db"),
        Path(fr"\\192.168.0.22\AutoStockBuyAndSell\ranking\ranking{today}.db"),
        Path(fr"\\192.168.0.22\AutoStockBuyAndSell\raw_data\ranking\ranking{today}.db"),
    ]
    out.extend(nas_candidates)

    # 4) legacy fallback
    out.append(LEGACY_RANKING_DB)

    # dedupe
    deduped: list[Path] = []
    seen = set()
    for p in out:
        try:
            s = str(p)
            if s not in seen:
                seen.add(s)
                deduped.append(Path(s))
        except Exception:
            continue

    return deduped


def _resolve_ranking_db_path() -> Optional[Path]:
    for p in _candidate_ranking_paths():
        try:
            if p.exists():
                logger.info("[RANKING LOADER] ranking DB resolved: %s", p)
                return p
        except Exception:
            continue

    logger.warning("[RANKING LOADER] ranking DB not found in candidates")
    return None


# ============================================================
# sqlite helpers
# ============================================================

def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass
    return conn


def _table_exists_conn(conn, table_name: str) -> bool:
    try:
        sql = """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name=?
            LIMIT 1
        """
        cur = conn.execute(sql, (table_name,))
        row = cur.fetchone()
        return row is not None
    except Exception:
        logger.debug("[RANKING LOADER] table_exists failed: %s", table_name, exc_info=True)
        return False


def _read_sql_df(sql: str, params: Optional[Iterable[Any]] = None) -> pd.DataFrame:
    params = tuple(params or ())

    # 1) SQLAlchemy engine 優先
    engine = _resolve_ranking_engine()
    if engine is not None:
        try:
            with engine.connect() as conn:
                return pd.read_sql(sql, conn, params=params)
        except Exception:
            logger.debug("[RANKING LOADER] read via engine failed", exc_info=True)

    # 2) sqlite path fallback
    db_path = _resolve_ranking_db_path()
    if db_path is None:
        logger.warning("ranking DB not found")
        return pd.DataFrame()

    try:
        with _connect_sqlite(db_path) as conn:
            return pd.read_sql(sql, conn, params=params)
    except Exception:
        logger.exception("[RANKING LOADER] read_sql failed path=%s", db_path)
        return pd.DataFrame()


def _table_exists(table_name: str) -> bool:
    engine = _resolve_ranking_engine()
    if engine is not None:
        try:
            with engine.connect() as conn:
                sql = """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table' AND name=?
                    LIMIT 1
                """
                row = conn.exec_driver_sql(sql, (table_name,)).fetchone()
                return row is not None
        except Exception:
            logger.debug("[RANKING LOADER] engine table_exists failed: %s", table_name, exc_info=True)

    db_path = _resolve_ranking_db_path()
    if db_path is None:
        return False

    try:
        with _connect_sqlite(db_path) as conn:
            return _table_exists_conn(conn, table_name)
    except Exception:
        logger.debug("[RANKING LOADER] sqlite table_exists failed: %s", table_name, exc_info=True)
        return False


# ============================================================
# dataframe utils
# ============================================================

def _extract_symbol_list(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty:
        return []

    for col in ("symbol", "code", "ticker", "stock_code"):
        if col in df.columns:
            try:
                values = (
                    df[col]
                    .astype(str)
                    .str.strip()
                    .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
                    .dropna()
                    .tolist()
                )
                # dedupe preserve order
                out = []
                seen = set()
                for x in values:
                    if x not in seen:
                        seen.add(x)
                        out.append(x)
                return out
            except Exception:
                logger.exception("[RANKING LOADER] symbol extraction failed col=%s", col)
                return []

    return []


# ============================================================
# legacy table loaders
# ============================================================

def _load_rank_from_db(sql: str, params: Optional[Iterable[Any]] = None) -> list[str]:
    df = _read_sql_df(sql, params=params)
    return _extract_symbol_list(df)


# ============================================================
# snapshot fallback helpers
# ============================================================

def _resolve_snapshot_table() -> Optional[str]:
    for table_name in (
        "ranking_snapshot_1min",
        "ranking_snapshot",
        "ranking_raw_1min",
        "ranking_raw",
    ):
        if _table_exists(table_name):
            logger.info("[RANKING LOADER] using snapshot table: %s", table_name)
            return table_name
    logger.warning("[RANKING LOADER] snapshot table not found")
    return None


def _resolve_snapshot_columns(table_name: str) -> dict[str, Optional[str]]:
    """
    列名ゆらぎを吸収するための簡易 resolver
    """
    cols: list[str] = []

    engine = _resolve_ranking_engine()
    if engine is not None:
        try:
            with engine.connect() as conn:
                rows = conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
                cols = [str(r[1]) for r in rows]
        except Exception:
            logger.debug("[RANKING LOADER] pragma via engine failed", exc_info=True)

    if not cols:
        db_path = _resolve_ranking_db_path()
        if db_path is not None:
            try:
                with _connect_sqlite(db_path) as conn:
                    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                    cols = [str(r[1]) for r in rows]
            except Exception:
                logger.debug("[RANKING LOADER] pragma via sqlite failed", exc_info=True)

    colset = set(cols)

    def pick(*names: str) -> Optional[str]:
        for n in names:
            if n in colset:
                return n
        return None

    resolved = {
        "symbol": pick("symbol", "code", "ticker", "stock_code"),
        "market": pick("market", "market_name", "market_type", "market_segment"),
        "rise": pick("rise_rate", "change_rate", "pct_change", "price_change_rate", "rate"),
        "fall": pick("fall_rate", "change_rate", "pct_change", "price_change_rate", "rate"),
        "value": pick("trading_value", "turnover", "value", "sell_buy_value", "volume_value"),
        "datetime": pick("datetime", "snapshot_time", "created_at", "updated_at"),
    }

    logger.info("[RANKING LOADER] snapshot columns resolved: %s", resolved)
    return resolved


def _load_from_snapshot_for_rise(limit: int = 50) -> list[str]:
    table = _resolve_snapshot_table()
    if not table:
        return []

    cols = _resolve_snapshot_columns(table)
    symbol_col = cols["symbol"]
    rise_col = cols["rise"]

    if not symbol_col or not rise_col:
        logger.warning("[RANKING LOADER] rise fallback skipped: unresolved columns")
        return []

    sql = f"""
        SELECT {symbol_col} AS symbol
        FROM {table}
        WHERE {symbol_col} IS NOT NULL
          AND {rise_col} IS NOT NULL
        ORDER BY CAST({rise_col} AS REAL) DESC
        LIMIT ?
    """
    return _load_rank_from_db(sql, params=(int(limit),))


def _load_from_snapshot_for_fall(limit: int = 50) -> list[str]:
    table = _resolve_snapshot_table()
    if not table:
        return []

    cols = _resolve_snapshot_columns(table)
    symbol_col = cols["symbol"]
    fall_col = cols["fall"]

    if not symbol_col or not fall_col:
        logger.warning("[RANKING LOADER] fall fallback skipped: unresolved columns")
        return []

    sql = f"""
        SELECT {symbol_col} AS symbol
        FROM {table}
        WHERE {symbol_col} IS NOT NULL
          AND {fall_col} IS NOT NULL
        ORDER BY CAST({fall_col} AS REAL) ASC
        LIMIT ?
    """
    return _load_rank_from_db(sql, params=(int(limit),))


def _load_from_snapshot_for_value(market: str, limit: int = 50) -> list[str]:
    table = _resolve_snapshot_table()
    if not table:
        return []

    cols = _resolve_snapshot_columns(table)
    symbol_col = cols["symbol"]
    value_col = cols["value"]
    market_col = cols["market"]

    if not symbol_col or not value_col:
        logger.warning("[RANKING LOADER] value fallback skipped: unresolved columns")
        return []

    market_norm = _normalize_market(market)

    if market_col and market_norm != "ALL":
        sql = f"""
            SELECT {symbol_col} AS symbol
            FROM {table}
            WHERE {symbol_col} IS NOT NULL
              AND {value_col} IS NOT NULL
              AND UPPER(CAST({market_col} AS TEXT)) = ?
            ORDER BY CAST({value_col} AS REAL) DESC
            LIMIT ?
        """
        return _load_rank_from_db(sql, params=(market_norm, int(limit)))

    sql = f"""
        SELECT {symbol_col} AS symbol
        FROM {table}
        WHERE {symbol_col} IS NOT NULL
          AND {value_col} IS NOT NULL
        ORDER BY CAST({value_col} AS REAL) DESC
        LIMIT ?
    """
    return _load_rank_from_db(sql, params=(int(limit),))


# ------------------------------------------------
# 全市場 値上がり
# ------------------------------------------------
def load_market_rise_rank(limit: int = 50) -> list[str]:
    """
    全市場 値上がり率ランキング
    旧 rank_rise テーブル優先、無ければ ranking_snapshot_1min fallback。
    """
    limit = int(limit)

    if _table_exists("rank_rise"):
        sql = """
            SELECT symbol
            FROM rank_rise
            ORDER BY rank ASC
            LIMIT ?
        """
        symbols = _load_rank_from_db(sql, params=(limit,))
    else:
        symbols = _load_from_snapshot_for_rise(limit=limit)

    logger.info("rise rank loaded: %s", len(symbols))
    return symbols


# ------------------------------------------------
# 全市場 値下がり
# ------------------------------------------------
def load_market_fall_rank(limit: int = 50) -> list[str]:
    """
    全市場 値下がり率ランキング
    旧 rank_fall テーブル優先、無ければ ranking_snapshot_1min fallback。
    """
    limit = int(limit)

    if _table_exists("rank_fall"):
        sql = """
            SELECT symbol
            FROM rank_fall
            ORDER BY rank ASC
            LIMIT ?
        """
        symbols = _load_rank_from_db(sql, params=(limit,))
    else:
        symbols = _load_from_snapshot_for_fall(limit=limit)

    logger.info("fall rank loaded: %s", len(symbols))
    return symbols


# ------------------------------------------------
# 市場別 売買代金
# ------------------------------------------------
def load_value_rank_by_market(market: str, limit: int = 50) -> list[str]:
    """
    市場別 売買代金ランキング
    market: "GROWTH" / "STANDARD" / "PRIME" / "ALL"
    旧 rank_value テーブル優先、無ければ ranking_snapshot_1min fallback。
    """
    market_norm = _normalize_market(market)
    limit = int(limit)

    if _table_exists("rank_value"):
        if market_norm == "ALL":
            sql = """
                SELECT symbol
                FROM rank_value
                ORDER BY rank ASC
                LIMIT ?
            """
            symbols = _load_rank_from_db(sql, params=(limit,))
        else:
            sql = """
                SELECT symbol
                FROM rank_value
                WHERE UPPER(CAST(market AS TEXT)) = ?
                ORDER BY rank ASC
                LIMIT ?
            """
            symbols = _load_rank_from_db(sql, params=(market_norm, limit))
    else:
        symbols = _load_from_snapshot_for_value(market=market_norm, limit=limit)

    logger.info("value rank loaded: %s %s", market_norm, len(symbols))
    return symbols