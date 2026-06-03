# ============================================================
# File   : core/startup/ranking_entry_source_db_fallback_patch.py
# Version: V3-RANKING-ENTRY-SOURCE-DB-FALLBACK-FAST-ROWID
# ------------------------------------------------------------
# 目的:
#   trading.ranking.entry_from_ranking が global_data.latest_ranking_* を
#   見つけられない場合でも、当日 rankingYYYYMMDD.db の
#   ranking_snapshot_1min から直接ランキングDFを復元して、
#   ranking entry loop を止めない。
#
# V3 重要修正:
#   - NAS上SQLiteで MAX(datetime) + ORDER BY datetime が100秒超になるケースを防ぐ。
#   - DB fallback は rowid DESC LIMIT のみで軽く読み、pandas側で最新近辺へ絞る。
#   - SQLite timeout / busy_timeout を短くし、詰まる場合は即スキップ。
#   - global_data direct source があればDBを読まない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_LAST_DB_DF: pd.DataFrame | None = None
_LAST_DB_TS: float = 0.0
_LAST_DB_KEY: tuple[str, str] | None = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _resolve_ranking_db_path() -> Optional[str]:
    try:
        from ats.ats_ranking.db_path import resolve_ranking_db_path
        p = resolve_ranking_db_path()
        if p:
            return str(p)
    except Exception as e:
        logger.debug("[RANKING ENTRY SOURCE DB FALLBACK] ats resolve failed: %s", e, exc_info=False)

    for key in ("RANKING_DB_PATH", "ATS_RANKING_DB_PATH", "KABU_RANKING_DB_PATH"):
        try:
            p = os.environ.get(key, "").strip()
            if p:
                return p
        except Exception:
            pass

    try:
        today = dt.datetime.now().strftime("%Y%m%d")
        p = rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking{today}.db"
        if os.path.exists(p):
            return p
    except Exception:
        pass
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()]
    except Exception:
        return []


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone() is not None
    except Exception:
        return False


def _find_col(cols: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    lower = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in cols:
            return cand
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def _read_global_data_source() -> pd.DataFrame:
    """既存 wrapper を呼ばず、global_data の実体だけを直接読む。"""
    try:
        from global_state import global_data
    except Exception:
        return pd.DataFrame()

    try:
        snapshot = getattr(global_data, "latest_ranking_snapshot", None)
        if isinstance(snapshot, list) and snapshot:
            df = pd.DataFrame(snapshot)
            if not df.empty:
                logger.info("[RANKING ENTRY SOURCE DB FALLBACK] direct source=latest_ranking_snapshot rows=%s", len(df))
                return df
    except Exception as e:
        logger.debug("[RANKING ENTRY SOURCE DB FALLBACK] direct snapshot read failed: %s", e, exc_info=False)

    for name in (
        "latest_ranking_raw",
        "latest_ranking_df",
        "ranking_raw_df",
        "ranking_snapshot_df",
        "ranking_df",
    ):
        try:
            df = getattr(global_data, name, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                logger.info("[RANKING ENTRY SOURCE DB FALLBACK] direct source=%s rows=%s", name, len(df))
                return df.copy()
        except Exception as e:
            logger.debug("[RANKING ENTRY SOURCE DB FALLBACK] direct attr read failed name=%s err=%s", name, e, exc_info=False)
    return pd.DataFrame()


def _cache_get(db_path: str, table: str) -> pd.DataFrame:
    global _LAST_DB_DF, _LAST_DB_TS, _LAST_DB_KEY
    ttl = _env_float("RANKING_ENTRY_SOURCE_DB_CACHE_TTL_SEC", 20.0)
    if _LAST_DB_DF is not None and _LAST_DB_KEY == (db_path, table) and (time.time() - _LAST_DB_TS) <= ttl:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] cache hit rows=%s age=%.1fs", len(_LAST_DB_DF), time.time() - _LAST_DB_TS)
        return _LAST_DB_DF.copy()
    return pd.DataFrame()


def _cache_put(db_path: str, table: str, df: pd.DataFrame) -> None:
    global _LAST_DB_DF, _LAST_DB_TS, _LAST_DB_KEY
    try:
        if isinstance(df, pd.DataFrame) and not df.empty:
            _LAST_DB_DF = df.copy()
            _LAST_DB_TS = time.time()
            _LAST_DB_KEY = (db_path, table)
    except Exception:
        pass


def _trim_recent(df: pd.DataFrame, dt_col: Optional[str], lookback_min: int, max_rows: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if dt_col and dt_col in df.columns:
        tmp = df.copy()
        tmp[dt_col] = pd.to_datetime(tmp[dt_col], errors="coerce")
        tmp = tmp.dropna(subset=[dt_col])
        if not tmp.empty:
            latest = tmp[dt_col].max()
            cutoff = latest - pd.Timedelta(minutes=max(1, lookback_min))
            recent = tmp[tmp[dt_col] >= cutoff]
            if not recent.empty:
                return recent.sort_values(dt_col, ascending=False, kind="stable").head(max_rows).copy()
            return tmp.sort_values(dt_col, ascending=False, kind="stable").head(max_rows).copy()
    return df.head(max_rows).copy()


def _load_from_ranking_db() -> pd.DataFrame:
    if not _env_bool("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", True):
        return pd.DataFrame()

    db_path = _resolve_ranking_db_path()
    if not db_path or not os.path.exists(db_path):
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db missing path=%s", db_path)
        return pd.DataFrame()

    table = os.environ.get("RANKING_ENTRY_SOURCE_DB_TABLE", "ranking_snapshot_1min").strip() or "ranking_snapshot_1min"
    lookback_min = max(1, _env_int("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", 4))
    max_rows = max(100, _env_int("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", 600))
    scan_rows = max(max_rows, _env_int("RANKING_ENTRY_SOURCE_DB_SCAN_ROWS", 900))
    sqlite_timeout = max(0.2, _env_float("RANKING_ENTRY_SOURCE_DB_SQLITE_TIMEOUT_SEC", 1.2))
    busy_timeout_ms = max(200, int(_env_float("RANKING_ENTRY_SOURCE_DB_BUSY_TIMEOUT_MS", 900.0)))

    cached = _cache_get(str(db_path), table)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached

    t0 = time.perf_counter()
    try:
        with sqlite3.connect(str(db_path), timeout=sqlite_timeout) as conn:
            try:
                conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass
            if not _table_exists(conn, table):
                logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] table missing db=%s table=%s", db_path, table)
                return pd.DataFrame()

            cols = _table_columns(conn, table)
            if not cols:
                return pd.DataFrame()
            dt_col = _find_col(cols, ("datetime", "snapshot_time", "time", "created_at"))

            # 重要: NAS SQLiteで MAX(datetime) / ORDER BY datetime を使うと100秒超になることがある。
            # rowid DESC LIMIT は末尾追加テーブルなら軽く、取得後にpandas側で最新時刻へ絞る。
            sql = f"SELECT * FROM {_quote(table)} ORDER BY rowid DESC LIMIT ?"
            raw = pd.read_sql_query(sql, conn, params=(scan_rows,))
            if raw is None or raw.empty:
                logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db read empty db=%s table=%s", db_path, table)
                return pd.DataFrame()

            df = _trim_recent(raw, dt_col, lookback_min, max_rows)
            if df is None or df.empty:
                logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] recent trim empty db=%s table=%s raw_rows=%s", db_path, table, len(raw))
                return pd.DataFrame()

            elapsed = time.perf_counter() - t0
            logger.warning(
                "[RANKING ENTRY SOURCE DB FALLBACK] loaded fast db=%s table=%s raw_rows=%s rows=%s lookback_min=%s scan_rows=%s max_rows=%s dt_col=%s elapsed=%.3fs cols=%s",
                db_path,
                table,
                len(raw),
                len(df),
                lookback_min,
                scan_rows,
                max_rows,
                dt_col,
                elapsed,
                list(df.columns)[:20],
            )
            if elapsed > _env_float("RANKING_ENTRY_SOURCE_DB_WARN_SEC", 3.0):
                logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] slow read warning elapsed=%.3fs db=%s table=%s", elapsed, db_path, table)
            _cache_put(str(db_path), table, df)
            return df
    except Exception as e:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db load failed db=%s table=%s err=%s", db_path, table, e, exc_info=False)
        return pd.DataFrame()


def _patched_get_ranking_source_df():
    df = _read_global_data_source()
    if isinstance(df, pd.DataFrame) and not df.empty:
        return df

    fb = _load_from_ranking_db()
    if isinstance(fb, pd.DataFrame) and not fb.empty:
        try:
            from global_state import global_data
            setattr(global_data, "latest_ranking_df", fb.copy())
        except Exception:
            pass
        return fb

    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] ranking source dataframe not found after direct+db fallback")
    return None


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.ranking.entry_from_ranking as mod
    except Exception as e:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] import failed: %s", e, exc_info=False)
        return False

    old = getattr(mod, "_get_ranking_source_df", None)
    if getattr(old, "_ranking_entry_source_db_fallback_v3", False):
        _INSTALLED = True
        return True

    _patched_get_ranking_source_df._ranking_entry_source_db_fallback = True  # type: ignore[attr-defined]
    _patched_get_ranking_source_df._ranking_entry_source_db_fallback_v3 = True  # type: ignore[attr-defined]
    _patched_get_ranking_source_df._original = None  # type: ignore[attr-defined]
    mod._get_ranking_source_df = _patched_get_ranking_source_df
    _INSTALLED = True
    logger.warning(
        "[RANKING ENTRY SOURCE DB FALLBACK] installed v3 fast_rowid=True enabled=%s lookback_min=%s max_rows=%s scan_rows=%s cache_ttl=%.1f",
        _env_bool("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", True),
        _env_int("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", 4),
        _env_int("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", 600),
        _env_int("RANKING_ENTRY_SOURCE_DB_SCAN_ROWS", 900),
        _env_float("RANKING_ENTRY_SOURCE_DB_CACHE_TTL_SEC", 20.0),
    )
    return True


try:
    install()
except Exception as e:
    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] auto install failed: %s", e, exc_info=False)
