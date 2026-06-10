# ============================================================
# File   : core/startup/ranking_entry_source_db_fallback_patch.py
# Version: V4-RANKING-ENTRY-SOURCE-DB-FALLBACK-STALE-REFRESH
# ------------------------------------------------------------
# Purpose:
#   Keep ranking_entry from stopping when global_data.latest_ranking_* is empty
#   OR stale.  A stale in-memory ranking snapshot is refreshed from today's
#   rankingYYYYMMDD.db using a cheap rowid DESC read.  If the DB itself is
#   delayed but still has same-day rows, return the rows fail-open so the final
#   entry/liquidity/board guards can make the real decision.
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
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
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


def _first_datetime_col(df: pd.DataFrame) -> Optional[str]:
    for c in ("datetime", "snapshot_time", "time", "created_at", "received_at", "inserted_at", "date"):
        if c in df.columns:
            return c
    return None


def _latest_info(df: pd.DataFrame) -> tuple[Optional[dt.datetime], Optional[float], str]:
    try:
        if df is None or df.empty:
            return None, None, "empty"
        col = _first_datetime_col(df)
        if not col:
            return None, None, "no_datetime_col"
        s = pd.to_datetime(df[col], errors="coerce").dropna()
        if s.empty:
            return None, None, f"bad_datetime_col={col}"
        latest = s.max().to_pydatetime().replace(tzinfo=None)
        age = max(0.0, (dt.datetime.now() - latest).total_seconds())
        return latest, age, col
    except Exception:
        return None, None, "latest_error"


def _fresh_enough(df: pd.DataFrame, *, label: str) -> bool:
    latest, age, col = _latest_info(df)
    max_age = max(30.0, _env_float("RANKING_ENTRY_SOURCE_MAX_AGE_SEC", 300.0))
    if age is not None and age <= max_age:
        logger.info("[RANKING ENTRY SOURCE DB FALLBACK] fresh source=%s rows=%s latest=%s age=%.1fs col=%s max_age=%.1fs", label, len(df), latest, age, col, max_age)
        return True
    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] stale/unknown source=%s rows=%s latest=%s age=%s col=%s max_age=%.1fs -> try DB refresh", label, 0 if df is None else len(df), latest, None if age is None else round(age, 1), col, max_age)
    return False


def _read_global_data_source() -> pd.DataFrame:
    try:
        from global_state import global_data
    except Exception:
        return pd.DataFrame()

    try:
        snapshot = getattr(global_data, "latest_ranking_snapshot", None)
        if isinstance(snapshot, list) and snapshot:
            df = pd.DataFrame(snapshot)
            if not df.empty:
                df.attrs["_ranking_source_name"] = "latest_ranking_snapshot"
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
                out = df.copy()
                out.attrs["_ranking_source_name"] = name
                return out
        except Exception as e:
            logger.debug("[RANKING ENTRY SOURCE DB FALLBACK] direct attr read failed name=%s err=%s", name, e, exc_info=False)
    return pd.DataFrame()


def _cache_get(db_path: str, table: str) -> pd.DataFrame:
    global _LAST_DB_DF, _LAST_DB_TS, _LAST_DB_KEY
    ttl = _env_float("RANKING_ENTRY_SOURCE_DB_CACHE_TTL_SEC", 10.0)
    if _LAST_DB_DF is not None and _LAST_DB_KEY == (db_path, table) and (time.time() - _LAST_DB_TS) <= ttl:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] cache hit table=%s rows=%s age=%.1fs", table, len(_LAST_DB_DF), time.time() - _LAST_DB_TS)
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


def _candidate_tables(conn: sqlite3.Connection) -> list[str]:
    configured = [x.strip() for x in os.environ.get("RANKING_ENTRY_SOURCE_DB_TABLES", "").split(",") if x.strip()]
    base = configured or [
        os.environ.get("RANKING_ENTRY_SOURCE_DB_TABLE", "ranking_snapshot_1min").strip() or "ranking_snapshot_1min",
        "ranking_snapshot",
        "ranking_raw_1min",
        "ranking_raw",
        "ranking_summary_1min",
        "ranking_summary_5min",
    ]
    out: list[str] = []
    for t in base:
        if t and t not in out and _table_exists(conn, t):
            out.append(t)
    return out


def _load_from_ranking_db() -> pd.DataFrame:
    if not _env_bool("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", True):
        return pd.DataFrame()

    db_path = _resolve_ranking_db_path()
    if not db_path or not os.path.exists(db_path):
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db missing path=%s", db_path)
        return pd.DataFrame()

    lookback_min = max(1, _env_int("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", 30))
    max_rows = max(100, _env_int("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", 900))
    scan_rows = max(max_rows, _env_int("RANKING_ENTRY_SOURCE_DB_SCAN_ROWS", 1500))
    sqlite_timeout = max(0.2, _env_float("RANKING_ENTRY_SOURCE_DB_SQLITE_TIMEOUT_SEC", 1.2))
    busy_timeout_ms = max(200, int(_env_float("RANKING_ENTRY_SOURCE_DB_BUSY_TIMEOUT_MS", 900.0)))

    t0 = time.perf_counter()
    best = pd.DataFrame()
    best_table = ""
    try:
        with sqlite3.connect(str(db_path), timeout=sqlite_timeout) as conn:
            try:
                conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass

            tables = _candidate_tables(conn)
            if not tables:
                logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] no candidate table db=%s", db_path)
                return pd.DataFrame()

            for table in tables:
                cached = _cache_get(str(db_path), table)
                if isinstance(cached, pd.DataFrame) and not cached.empty:
                    return cached
                cols = _table_columns(conn, table)
                if not cols:
                    continue
                dt_col = _find_col(cols, ("datetime", "snapshot_time", "time", "created_at", "received_at", "inserted_at", "date"))
                try:
                    raw = pd.read_sql_query(f"SELECT * FROM {_quote(table)} ORDER BY rowid DESC LIMIT ?", conn, params=(scan_rows,))
                except Exception as e:
                    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] table read failed table=%s err=%s", table, e, exc_info=False)
                    continue
                if raw is None or raw.empty:
                    continue
                df = _trim_recent(raw, dt_col, lookback_min, max_rows)
                if df.empty:
                    continue
                latest, age, col = _latest_info(df)
                logger.warning(
                    "[RANKING ENTRY SOURCE DB FALLBACK] table candidate db=%s table=%s raw_rows=%s rows=%s latest=%s age=%s col=%s lookback_min=%s scan_rows=%s",
                    db_path, table, len(raw), len(df), latest, None if age is None else round(age, 1), col, lookback_min, scan_rows,
                )
                if best.empty:
                    best = df
                    best_table = table
                else:
                    b_latest, _b_age, _ = _latest_info(best)
                    if latest is not None and (b_latest is None or latest > b_latest):
                        best = df
                        best_table = table

        if best is None or best.empty:
            logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db read empty db=%s", db_path)
            return pd.DataFrame()

        latest, age, col = _latest_info(best)
        elapsed = time.perf_counter() - t0
        max_age = max(30.0, _env_float("RANKING_ENTRY_SOURCE_DB_MAX_AGE_SEC", 300.0))
        failopen_max_age = max(max_age, _env_float("RANKING_ENTRY_SOURCE_DB_FAILOPEN_MAX_AGE_SEC", 1800.0))
        same_day = bool(latest is not None and latest.date() == dt.datetime.now().date())
        stale = bool(age is None or age > max_age)
        allow_stale = bool(stale and same_day and age is not None and age <= failopen_max_age and _env_bool("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE", True))

        if stale and not allow_stale:
            logger.warning(
                "[RANKING ENTRY SOURCE DB FALLBACK] db latest stale and not allowed table=%s rows=%s latest=%s age=%s max_age=%.1fs failopen_max=%.1fs same_day=%s",
                best_table, len(best), latest, None if age is None else round(age, 1), max_age, failopen_max_age, same_day,
            )
            return pd.DataFrame()

        logger.warning(
            "[RANKING ENTRY SOURCE DB FALLBACK] loaded v4 db=%s table=%s rows=%s latest=%s age=%s stale=%s allow_stale=%s elapsed=%.3fs cols=%s",
            db_path, best_table, len(best), latest, None if age is None else round(age, 1), stale, allow_stale, elapsed, list(best.columns)[:20],
        )
        _cache_put(str(db_path), best_table, best)
        return best
    except Exception as e:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db load failed db=%s err=%s", db_path, e, exc_info=False)
        return pd.DataFrame()


def _publish_to_global_data(df: pd.DataFrame, *, source: str) -> None:
    try:
        from global_state import global_data
        out = df.copy()
        setattr(global_data, "latest_ranking_df", out)
        setattr(global_data, "ranking_snapshot_df", out)
        try:
            setattr(global_data, "latest_ranking_snapshot", out.to_dict("records"))
        except Exception:
            pass
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] published source=%s rows=%s latest=%s", source, len(out), _latest_info(out)[0])
    except Exception:
        pass


def _patched_get_ranking_source_df():
    direct = _read_global_data_source()
    direct_name = str(getattr(direct, "attrs", {}).get("_ranking_source_name", "global_data")) if isinstance(direct, pd.DataFrame) else "global_data"

    if isinstance(direct, pd.DataFrame) and not direct.empty and _fresh_enough(direct, label=direct_name):
        return direct

    fb = _load_from_ranking_db()
    if isinstance(fb, pd.DataFrame) and not fb.empty:
        _publish_to_global_data(fb, source="db_fallback_v4")
        return fb

    if isinstance(direct, pd.DataFrame) and not direct.empty and _env_bool("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL", True):
        latest, age, col = _latest_info(direct)
        max_age = _env_float("RANKING_ENTRY_SOURCE_FAILOPEN_GLOBAL_MAX_AGE_SEC", 1800.0)
        same_day = latest is not None and latest.date() == dt.datetime.now().date()
        if same_day and age is not None and age <= max_age:
            logger.warning(
                "[RANKING ENTRY SOURCE DB FALLBACK] fail-open stale global source=%s rows=%s latest=%s age=%.1fs col=%s max_age=%.1fs",
                direct_name, len(direct), latest, age, col, max_age,
            )
            return direct

    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] ranking source dataframe not found after direct+db fallback")
    return None


def install() -> bool:
    global _INSTALLED
    try:
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", "1")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_MAX_AGE_SEC", "300")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_MAX_AGE_SEC", "300")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE", "1")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_FAILOPEN_MAX_AGE_SEC", "1800")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL", "1")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_FAILOPEN_GLOBAL_MAX_AGE_SEC", "1800")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", "30")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", "900")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_SCAN_ROWS", "1500")

        import trading.ranking.entry_from_ranking as mod
        cur = getattr(mod, "_get_ranking_source_df", None)
        if getattr(cur, "_ranking_entry_source_db_fallback_v4", False):
            _INSTALLED = True
            return True
        _patched_get_ranking_source_df._ranking_entry_source_db_fallback = True  # type: ignore[attr-defined]
        _patched_get_ranking_source_df._ranking_entry_source_db_fallback_v4 = True  # type: ignore[attr-defined]
        _patched_get_ranking_source_df._original = getattr(cur, "_original", cur)  # type: ignore[attr-defined]
        mod._get_ranking_source_df = _patched_get_ranking_source_df
        _INSTALLED = True
        logger.warning(
            "[RANKING ENTRY SOURCE DB FALLBACK] installed v4 stale_refresh=True enabled=%s max_age=%ss db_max_age=%ss failopen=%s failopen_max=%ss lookback=%s scan_rows=%s",
            os.environ.get("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED"),
            os.environ.get("RANKING_ENTRY_SOURCE_MAX_AGE_SEC"),
            os.environ.get("RANKING_ENTRY_SOURCE_DB_MAX_AGE_SEC"),
            os.environ.get("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE"),
            os.environ.get("RANKING_ENTRY_SOURCE_DB_FAILOPEN_MAX_AGE_SEC"),
            os.environ.get("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN"),
            os.environ.get("RANKING_ENTRY_SOURCE_DB_SCAN_ROWS"),
        )
        return True
    except Exception as e:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] install failed: %s", e, exc_info=False)
        return False


try:
    install()
except Exception as e:
    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] auto install failed: %s", e, exc_info=False)


__all__ = ["install"]
