# ============================================================
# File   : core/startup/ranking_entry_source_db_fallback_patch.py
# Version: V2-RANKING-ENTRY-SOURCE-DB-FALLBACK-NO-RECURSION
# ------------------------------------------------------------
# 目的:
#   trading.ranking.entry_from_ranking が global_data.latest_ranking_* を
#   見つけられない場合でも、当日 rankingYYYYMMDD.db の
#   ranking_snapshot_1min から直接ランキングDFを復元して、
#   ranking entry loop を止めない。
#
# V2 重要修正:
#   - ranking_entry_flat_price_guard_patch と相互に _get_ranking_source_df を
#     wrapper し、互いを original として呼んで RecursionError になる問題を防止。
#   - 既存 wrapper は呼ばず、global_data を直接読む → DB fallback の順で処理する。
#   - RecursionError中の logger.warning(..., exc_info=True) が logging 自体を
#     再帰させるため、例外ログは exc_info=False に抑制。
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
    if getattr(old, "_ranking_entry_source_db_fallback_v2", False):
        _INSTALLED = True
        return True

    _patched_get_ranking_source_df._ranking_entry_source_db_fallback = True  # type: ignore[attr-defined]
    _patched_get_ranking_source_df._ranking_entry_source_db_fallback_v2 = True  # type: ignore[attr-defined]
    _patched_get_ranking_source_df._original = None  # type: ignore[attr-defined]
    mod._get_ranking_source_df = _patched_get_ranking_source_df
    _INSTALLED = True
    logger.warning(
        "[RANKING ENTRY SOURCE DB FALLBACK] installed v2 no_recursion=True enabled=%s lookback_min=%s max_rows=%s",
        _env_bool("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", True),
        _env_int("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", 8),
        _env_int("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", 2000),
    )
    return True


try:
    install()
except Exception as e:
    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] auto install failed: %s", e, exc_info=False)


__all__ = ["install"]
