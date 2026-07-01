# ============================================================
# File   : core/startup/push_summary_db_source_patch.py
# Version: REV1-PUSH-SUMMARY-DB-SOURCE-FALLBACK
# ------------------------------------------------------------
# Purpose:
#   summary_database is a separate process from push_receiver.
#   Therefore global_data.push_df / stream_data in memory can be empty or stale
#   even while push_receiver is receiving and saving fresh PUSH ticks.
#
#   This patch is intentionally installed only by scripts/summary_database_runner.py.
#   It keeps push_receiver simple and makes summary_database read the latest
#   committed PUSH rows from pushYYYYMMDD.db when in-memory PUSH rows are empty
#   or older than the DB source.
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIGINAL_RESOLVE_PUSH_SOURCE_DF = None

_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _env_true(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _TRUE:
            return True
        if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = str(os.getenv(name, str(default))).strip()
        return int(float(raw))
    except Exception:
        return int(default)


def _safe_latest_dt(df: pd.DataFrame) -> pd.Timestamp | None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for col in ("datetime", "received_at", "last_tick_at", "first_tick_at"):
        if col not in df.columns:
            continue
        try:
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().any():
                latest = s.max()
                try:
                    latest = latest.tz_localize(None)
                except Exception:
                    pass
                return latest
        except Exception:
            pass
    return None


def _safe_symbol_count(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return 0
    try:
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _resolve_push_db_path() -> Path | None:
    try:
        from config.paths import get_path
        base_dir = Path(get_path("raw_push"))
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] get_path(raw_push) failed")
        return None

    today = dt.datetime.now().strftime("%Y%m%d")
    db_path = base_dir / f"push{today}.db"
    if not db_path.exists():
        logger.warning("[PUSH SUMMARY DB SOURCE] db not found path=%s", db_path)
        return None
    return db_path


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _read_push_db_recent_rows() -> pd.DataFrame:
    db_path = _resolve_push_db_path()
    if db_path is None:
        return pd.DataFrame()

    lookback_min = max(3, _env_int("PUSH_SUMMARY_DB_LOOKBACK_MIN", 20))
    max_rows = max(1000, _env_int("PUSH_SUMMARY_DB_MAX_ROWS", 30000))
    busy_timeout_ms = max(1000, _env_int("PUSH_SUMMARY_DB_BUSY_TIMEOUT_MS", 5000))

    now = dt.datetime.now()
    cutoff = (now - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=max(1.0, busy_timeout_ms / 1000.0))
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        conn.execute("PRAGMA query_only=ON")

        table_name = "stream_data_raw" if _table_exists(conn, "stream_data_raw") else "stream_data"
        if not _table_exists(conn, table_name):
            logger.warning("[PUSH SUMMARY DB SOURCE] no stream table db=%s", db_path)
            return pd.DataFrame()

        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
        colset = set(cols)

        select_cols: list[str] = []
        for c in (
            "symbol", "symbolname", "datetime", "date", "time", "price", "volume",
            "trading_value", "vwap", "previousclose", "opening_price", "high_price", "low_price",
            "raw_json", "received_at",
        ):
            if c in colset:
                select_cols.append(c)

        if "symbol" not in select_cols or "datetime" not in select_cols:
            logger.warning(
                "[PUSH SUMMARY DB SOURCE] required cols missing table=%s cols=%s",
                table_name,
                cols,
            )
            return pd.DataFrame()

        where_parts = ["datetime >= ?"]
        params: list[Any] = [cutoff]
        if "received_at" in colset:
            where_parts.append("received_at >= ?")
            params.append(cutoff)

        sql = (
            f"SELECT {', '.join(select_cols)} FROM {table_name} "
            f"WHERE ({' OR '.join(where_parts)}) "
            f"ORDER BY datetime DESC LIMIT ?"
        )
        params.append(max_rows)

        df = pd.read_sql_query(sql, conn, params=params)
        if df.empty:
            logger.warning(
                "[PUSH SUMMARY DB SOURCE] recent rows empty table=%s cutoff=%s lookback_min=%s db=%s",
                table_name,
                cutoff,
                lookback_min,
                db_path,
            )
            return df

        df = df.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

        # Normalize column names expected by push_summary_engine._normalize_push_source_df().
        if "price" in df.columns:
            df["current_price"] = pd.to_numeric(df["price"], errors="coerce")
            df["close"] = pd.to_numeric(df["price"], errors="coerce")
            df["close_price"] = pd.to_numeric(df["price"], errors="coerce")
        if "opening_price" in df.columns:
            df["open"] = pd.to_numeric(df["opening_price"], errors="coerce")
            df["open_price"] = pd.to_numeric(df["opening_price"], errors="coerce")
        if "high_price" in df.columns:
            df["high"] = pd.to_numeric(df["high_price"], errors="coerce")
        if "low_price" in df.columns:
            df["low"] = pd.to_numeric(df["low_price"], errors="coerce")
        if "volume" in df.columns:
            df["trading_volume"] = pd.to_numeric(df["volume"], errors="coerce")

        df["source"] = "push_db_recent"

        latest = _safe_latest_dt(df)
        logger.warning(
            "[PUSH SUMMARY DB SOURCE] loaded table=%s rows=%s symbols=%s latest_dt=%s cutoff=%s db=%s",
            table_name,
            len(df),
            _safe_symbol_count(df),
            latest,
            cutoff,
            db_path,
        )
        return df

    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] read failed db=%s", db_path)
        return pd.DataFrame()
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def install() -> bool:
    global _INSTALLED, _ORIGINAL_RESOLVE_PUSH_SOURCE_DF

    if _INSTALLED:
        return True

    if not _env_true("PUSH_SUMMARY_DB_SOURCE_FALLBACK", True):
        logger.warning("[PUSH SUMMARY DB SOURCE] disabled by PUSH_SUMMARY_DB_SOURCE_FALLBACK=0")
        return False

    try:
        import trading.summary.engine.push_summary_engine as engine
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] import push_summary_engine failed")
        return False

    original = getattr(engine, "_resolve_push_source_df", None)
    if not callable(original):
        logger.warning("[PUSH SUMMARY DB SOURCE] original _resolve_push_source_df missing")
        return False

    _ORIGINAL_RESOLVE_PUSH_SOURCE_DF = original

    def _resolve_push_source_df_patched() -> pd.DataFrame:
        mem_df = pd.DataFrame()
        try:
            mem_df = original()
        except Exception:
            logger.exception("[PUSH SUMMARY DB SOURCE] original resolver failed")

        db_df = _read_push_db_recent_rows()
        if not isinstance(db_df, pd.DataFrame) or db_df.empty:
            return mem_df if isinstance(mem_df, pd.DataFrame) else pd.DataFrame()

        if not isinstance(mem_df, pd.DataFrame) or mem_df.empty:
            logger.warning(
                "[PUSH SUMMARY DB SOURCE] use db because memory push_df empty rows=%s symbols=%s latest_dt=%s",
                len(db_df),
                _safe_symbol_count(db_df),
                _safe_latest_dt(db_df),
            )
            return db_df

        mem_latest = _safe_latest_dt(mem_df)
        db_latest = _safe_latest_dt(db_df)
        max_lag_sec = max(5, _env_int("PUSH_SUMMARY_DB_SOURCE_MAX_MEM_LAG_SEC", 30))

        try:
            if mem_latest is None or (db_latest is not None and (db_latest - mem_latest).total_seconds() > max_lag_sec):
                logger.warning(
                    "[PUSH SUMMARY DB SOURCE] use db because memory stale mem_rows=%s mem_latest=%s db_rows=%s db_latest=%s lag_limit=%ss",
                    len(mem_df),
                    mem_latest,
                    len(db_df),
                    db_latest,
                    max_lag_sec,
                )
                return db_df
        except Exception:
            logger.exception("[PUSH SUMMARY DB SOURCE] freshness compare failed; keep memory")

        logger.info(
            "[PUSH SUMMARY DB SOURCE] keep memory rows=%s latest=%s db_rows=%s db_latest=%s",
            len(mem_df),
            mem_latest,
            len(db_df),
            db_latest,
        )
        return mem_df

    engine._resolve_push_source_df = _resolve_push_source_df_patched  # type: ignore[attr-defined]
    _INSTALLED = True
    logger.warning("[PUSH SUMMARY DB SOURCE] installed")
    return True
