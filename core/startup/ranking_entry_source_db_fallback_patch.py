# ============================================================
# File   : core/startup/ranking_entry_source_db_fallback_patch.py
# Version: V1-RANKING-ENTRY-SOURCE-DB-FALLBACK
# ------------------------------------------------------------
# 目的:
#   trading.ranking.entry_from_ranking が global_data.latest_ranking_* を
#   見つけられない場合でも、当日 rankingYYYYMMDD.db の
#   ranking_snapshot_1min から直接ランキングDFを復元して、
#   ranking entry loop を止めない。
#
# 背景ログ:
#   [RANKING ENTRY LOOP] ranking source dataframe not found
#   [ATS RANKING] use preferred usable today db=...ranking20260529.db
#
# 方針:
#   - まず元の _get_ranking_source_df() を呼ぶ。
#   - None/empty の場合のみ DB fallback。
#   - 最新 timestamp から lookback 分以内、最大行数だけ読む。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_GET_SOURCE = None


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


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _resolve_ranking_db_path() -> Optional[str]:
    try:
        from ats.ats_ranking.db_path import resolve_ranking_db_path
        p = resolve_ranking_db_path()
        if p:
            return str(p)
    except Exception:
        logger.debug("[RANKING ENTRY SOURCE DB FALLBACK] ats resolve failed", exc_info=True)

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


def _load_from_ranking_db() -> pd.DataFrame:
    if not _env_bool("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", True):
        return pd.DataFrame()

    db_path = _resolve_ranking_db_path()
    if not db_path or not os.path.exists(db_path):
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db missing path=%s", db_path)
        return pd.DataFrame()

    table = os.environ.get("RANKING_ENTRY_SOURCE_DB_TABLE", "ranking_snapshot_1min").strip() or "ranking_snapshot_1min"
    lookback_min = max(1, _env_int("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", 8))
    max_rows = max(100, _env_int("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", 2000))

    try:
        with sqlite3.connect(str(db_path), timeout=20) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=20000")
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
            if dt_col:
                q_dt = _quote(dt_col)
                latest_row = conn.execute(f"SELECT MAX({q_dt}) FROM {_quote(table)} WHERE {q_dt} IS NOT NULL").fetchone()
                latest = str(latest_row[0]) if latest_row and latest_row[0] is not None else ""
                latest_ts = pd.to_datetime(latest, errors="coerce")
                if pd.isna(latest_ts):
                    sql = f"SELECT * FROM {_quote(table)} WHERE {q_dt} IS NOT NULL ORDER BY {q_dt} DESC LIMIT ?"
                    params: tuple[Any, ...] = (max_rows,)
                else:
                    cutoff = (latest_ts.to_pydatetime() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
                    sql = f"SELECT * FROM {_quote(table)} WHERE {q_dt} IS NOT NULL AND {q_dt} >= ? ORDER BY {q_dt} DESC LIMIT ?"
                    params = (cutoff, max_rows)
            else:
                sql = f"SELECT * FROM {_quote(table)} LIMIT ?"
                params = (max_rows,)

            df = pd.read_sql_query(sql, conn, params=params)
            if df is None or df.empty:
                logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db read empty db=%s table=%s", db_path, table)
                return pd.DataFrame()

            logger.warning(
                "[RANKING ENTRY SOURCE DB FALLBACK] loaded db=%s table=%s rows=%s lookback_min=%s max_rows=%s dt_col=%s cols=%s",
                db_path,
                table,
                len(df),
                lookback_min,
                max_rows,
                dt_col,
                list(df.columns)[:20],
            )
            return df
    except Exception:
        logger.exception("[RANKING ENTRY SOURCE DB FALLBACK] db load failed db=%s table=%s", db_path, table)
        return pd.DataFrame()


def install() -> bool:
    global _INSTALLED, _ORIG_GET_SOURCE
    if _INSTALLED:
        return True
    try:
        import trading.ranking.entry_from_ranking as mod
    except Exception:
        logger.exception("[RANKING ENTRY SOURCE DB FALLBACK] import failed")
        return False

    old = getattr(mod, "_get_ranking_source_df", None)
    if getattr(old, "_ranking_entry_source_db_fallback", False):
        _INSTALLED = True
        return True
    if not callable(old):
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] target unavailable")
        return False

    _ORIG_GET_SOURCE = old

    def _patched_get_ranking_source_df():
        try:
            df = _ORIG_GET_SOURCE()
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        except Exception:
            logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] original source failed -> fallback", exc_info=True)
        fb = _load_from_ranking_db()
        if isinstance(fb, pd.DataFrame) and not fb.empty:
            try:
                from global_state import global_data
                setattr(global_data, "latest_ranking_df", fb.copy())
            except Exception:
                pass
            return fb
        return None

    _patched_get_ranking_source_df._ranking_entry_source_db_fallback = True  # type: ignore[attr-defined]
    _patched_get_ranking_source_df._original = old  # type: ignore[attr-defined]
    mod._get_ranking_source_df = _patched_get_ranking_source_df
    _INSTALLED = True
    logger.warning(
        "[RANKING ENTRY SOURCE DB FALLBACK] installed enabled=%s lookback_min=%s max_rows=%s",
        _env_bool("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", True),
        _env_int("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", 8),
        _env_int("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", 2000),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY SOURCE DB FALLBACK] auto install failed")


__all__ = ["install"]
