# ============================================================
# File   : core/startup/push_summary_db_latest_source_patch.py
# Version: V1-PUSH-SUMMARY-DB-LATEST-SOURCE
# ------------------------------------------------------------
# 目的:
#   data_collectors_runner / summary_database 側で、process-local の
#   global_data.push_df が古いまま残り、PUSH DB には最新行が保存されているのに
#   1分PUSH summary が 08:59 等の stale データを再利用する問題を防ぐ。
#
# 事象ログ:
#   [PUSH DB WRITER] push_after=9819 ... latest_recv=09:07台
#   なのに
#   [PUSH SUMMARY STATS] resolved push source interval=1 latest_dt=08:59:00
#   [SUMMARY STALE DROP] latest_age_sec=480 ... after=0
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

VERSION = "V1-PUSH-SUMMARY-DB-LATEST-SOURCE"
_PATCHED = False
_ORIGINAL_RESOLVE_PUSH_SOURCE_DF = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _is_database_process() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in __import__("sys").argv)
        return any(x in argv for x in (
            "main_database.py",
            "data_collectors_runner.py",
            "summary_database_runner.py",
            "push_receiver_runner.py",
            "yahoo_complement_runner.py",
        )) or any(os.getenv(x, "").strip() == "1" for x in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        ))
    except Exception:
        return False


def _push_db_path() -> Path:
    ymd = os.getenv("KABU_TODAY") or os.getenv("TARGET_DATE") or dt.datetime.now().strftime("%Y%m%d")
    try:
        from config.paths import get_path
        root = Path(get_path("raw_push"))
    except Exception:
        root = Path(os.getenv("PUSH_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\push"))
    return root / f"push{ymd}.db"


def _latest_dt(df: Any):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "current_price_time", "CurrentPriceTime", "received_at", "time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce")
                if s.notna().any():
                    ts = s.max()
                    try:
                        return ts.tz_localize(None)
                    except Exception:
                        return ts
    except Exception:
        pass
    return None


def _age_sec(ts: Any) -> float:
    try:
        if ts is None or pd.isna(ts):
            return 999999.0
        t = pd.Timestamp(ts)
        try:
            t = t.tz_localize(None)
        except Exception:
            pass
        return max(0.0, (dt.datetime.now() - t.to_pydatetime()).total_seconds())
    except Exception:
        return 999999.0


def _read_push_db_recent() -> pd.DataFrame:
    db = _push_db_path()
    if not db.exists():
        return pd.DataFrame()
    lookback_min = max(1, _env_int("PUSH_SUMMARY_DB_SOURCE_LOOKBACK_MIN", 20))
    limit = max(100, _env_int("PUSH_SUMMARY_DB_SOURCE_LIMIT", 20000))
    busy_ms = max(100, _env_int("PUSH_SUMMARY_DB_SOURCE_BUSY_TIMEOUT_MS", 1000))
    cutoff = (dt.datetime.now() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with sqlite3.connect(str(db), timeout=max(0.5, busy_ms / 1000.0)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute(f"PRAGMA busy_timeout={busy_ms}")
            table = "stream_data_raw"
            try:
                n = conn.execute("SELECT 1 FROM stream_data_raw LIMIT 1").fetchone()
            except Exception:
                table = "stream_data"
            sql = f"""
                SELECT *
                  FROM {table}
                 WHERE datetime >= ?
                 ORDER BY datetime DESC
                 LIMIT ?
            """
            df = pd.read_sql_query(sql, conn, params=(cutoff, int(limit)))
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] read db failed db=%s", db)
        return pd.DataFrame()

    if df.empty:
        logger.warning("[PUSH SUMMARY DB SOURCE] db recent empty db=%s cutoff=%s table=stream_data/raw", db, cutoff)
        return df
    try:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime", "symbol"]).copy()
        df["symbol"] = df["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        if "price" in df.columns:
            df["current_price"] = pd.to_numeric(df.get("current_price", df["price"]), errors="coerce").combine_first(pd.to_numeric(df["price"], errors="coerce"))
            df["close"] = pd.to_numeric(df["price"], errors="coerce").combine_first(pd.to_numeric(df["current_price"], errors="coerce"))
        if "volume" in df.columns:
            df["trading_volume"] = pd.to_numeric(df.get("trading_volume", df["volume"]), errors="coerce").combine_first(pd.to_numeric(df["volume"], errors="coerce"))
        df["source"] = "push_stream_raw_db_latest_patch"
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] normalize failed")
    logger.warning(
        "[PUSH SUMMARY DB SOURCE] loaded db rows=%s symbols=%s latest_dt=%s db=%s version=%s",
        len(df), df["symbol"].nunique() if "symbol" in df.columns else 0, _latest_dt(df), db, VERSION,
    )
    return df.reset_index(drop=True)


def _better_df(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """Return fresher/non-empty source. b is DB source."""
    if not isinstance(b, pd.DataFrame) or b.empty:
        return a
    if not isinstance(a, pd.DataFrame) or a.empty:
        return b
    a_dt = _latest_dt(a)
    b_dt = _latest_dt(b)
    a_age = _age_sec(a_dt)
    b_age = _age_sec(b_dt)
    stale_sec = max(30.0, _env_float("PUSH_SUMMARY_DB_SOURCE_REPLACE_IF_OLDER_SEC", 180.0))
    if b_dt is not None and (a_dt is None or b_dt > a_dt or a_age > stale_sec):
        logger.warning(
            "[PUSH SUMMARY DB SOURCE] replace push source old_latest=%s old_age=%.1fs db_latest=%s db_age=%.1fs old_rows=%s db_rows=%s version=%s",
            a_dt, a_age, b_dt, b_age, len(a), len(b), VERSION,
        )
        return b
    return a


def _patched_resolve_push_source_df():
    base = pd.DataFrame()
    try:
        if callable(_ORIGINAL_RESOLVE_PUSH_SOURCE_DF):
            base = _ORIGINAL_RESOLVE_PUSH_SOURCE_DF()
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] original resolve failed")
    try:
        if not _env_bool("PUSH_SUMMARY_DB_SOURCE_ENABLED", True):
            return base
        db_df = _read_push_db_recent()
        return _better_df(base, db_df)
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] patched resolve failed")
        return base


def install() -> bool:
    global _PATCHED, _ORIGINAL_RESOLVE_PUSH_SOURCE_DF
    if not _env_bool("PUSH_SUMMARY_DB_SOURCE_ENABLED", True):
        logger.warning("[PUSH SUMMARY DB SOURCE] disabled by env")
        return False
    try:
        import trading.summary.engine.push_summary_engine as engine
        cur = getattr(engine, "_resolve_push_source_df", None)
        if getattr(cur, "_push_summary_db_latest_source_v1", False):
            _PATCHED = True
            return True
        if not callable(cur):
            logger.warning("[PUSH SUMMARY DB SOURCE] target missing")
            return False
        _ORIGINAL_RESOLVE_PUSH_SOURCE_DF = cur
        _patched_resolve_push_source_df._push_summary_db_latest_source_v1 = True  # type: ignore[attr-defined]
        _patched_resolve_push_source_df._original = cur  # type: ignore[attr-defined]
        engine._resolve_push_source_df = _patched_resolve_push_source_df
        _PATCHED = True
        logger.warning(
            "[PUSH SUMMARY DB SOURCE] installed ok=True db_process=%s db=%s version=%s",
            _is_database_process(), _push_db_path(), VERSION,
        )
        return True
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[PUSH SUMMARY DB SOURCE] auto install failed")


__all__ = ["install", "VERSION"]
