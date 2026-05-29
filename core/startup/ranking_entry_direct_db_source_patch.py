# ============================================================
# File   : core/startup/ranking_entry_direct_db_source_patch.py
# Version: V1-DIRECT-RANKING-DB-SOURCE
# ------------------------------------------------------------
# 目的:
#   main.py は split mode ではランキング取得/DB保存を担当しない。
#   そのため global_data.latest_ranking_df が空の時があり、
#   trading.ranking.entry_from_ranking._get_ranking_source_df() が
#     ranking source dataframe not found
#   で止まる。
#
# 方針:
#   - entry_from_ranking 本体の _get_ranking_source_df をwrapする。
#   - メモリDFが取れない場合だけ ranking_snapshot_1min から最新datetimeの行を読む。
#   - DB path は ats.ats_ranking.db_path.get_usable_ranking_db_path を優先。
#   - main_database.py が保存したDBを main.py のENTRY判定で読む構成に合わせる。
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_GETTER = None


def _resolve_ranking_db_path() -> str | None:
    try:
        from ats.ats_ranking.db_path import get_usable_ranking_db_path
        p = get_usable_ranking_db_path(
            force_refresh=True,
            allow_fallback=False,
            prefer_today_even_if_empty=True,
        )
        return str(p) if p else None
    except TypeError:
        try:
            from ats.ats_ranking.db_path import get_usable_ranking_db_path
            p = get_usable_ranking_db_path()
            return str(p) if p else None
        except Exception:
            logger.debug("[RANKING ENTRY DIRECT DB] db_path fallback failed", exc_info=True)
            return None
    except Exception:
        logger.debug("[RANKING ENTRY DIRECT DB] resolve db path failed", exc_info=True)
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _latest_df_from_db(path: str) -> pd.DataFrame:
    if not path:
        return pd.DataFrame()
    if not Path(path).exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            table = "ranking_snapshot_1min"
            if not _table_exists(conn, table):
                logger.warning("[RANKING ENTRY DIRECT DB] table missing path=%s table=%s", path, table)
                return pd.DataFrame()
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            dt_col = "datetime" if "datetime" in cols else ("snapshot_time" if "snapshot_time" in cols else None)
            if not dt_col:
                df = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 2000", conn)
                logger.warning("[RANKING ENTRY DIRECT DB] loaded without dt_col rows=%s path=%s", len(df), path)
                return df
            latest = conn.execute(f"SELECT MAX({dt_col}) FROM {table}").fetchone()[0]
            if not latest:
                return pd.DataFrame()
            df = pd.read_sql_query(f"SELECT * FROM {table} WHERE {dt_col} = ?", conn, params=(latest,))
            logger.warning(
                "[RANKING ENTRY DIRECT DB] loaded latest snapshot rows=%s dt_col=%s latest=%s path=%s",
                len(df), dt_col, latest, path,
            )
            return df
    except Exception:
        logger.exception("[RANKING ENTRY DIRECT DB] load latest snapshot failed path=%s", path)
        return pd.DataFrame()


def _patched_get_ranking_source_df():
    try:
        if callable(_ORIG_GETTER):
            df = _ORIG_GETTER()
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
    except Exception:
        logger.warning("[RANKING ENTRY DIRECT DB] original getter failed -> try db", exc_info=True)

    path = _resolve_ranking_db_path()
    df = _latest_df_from_db(path or "")
    if isinstance(df, pd.DataFrame) and not df.empty:
        logger.warning("[RANKING ENTRY DIRECT DB] source=db rows=%s cols=%s", len(df), len(df.columns))
        return df
    logger.warning("[RANKING ENTRY DIRECT DB] db fallback empty path=%s", path)
    return None


def install() -> bool:
    global _INSTALLED, _ORIG_GETTER
    if _INSTALLED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        cur = getattr(efr, "_get_ranking_source_df", None)
        if not callable(cur):
            logger.warning("[RANKING ENTRY DIRECT DB] target missing")
            return False
        if getattr(cur, "_ranking_entry_direct_db_source_patch", False):
            _INSTALLED = True
            return True
        _ORIG_GETTER = cur
        _patched_get_ranking_source_df._ranking_entry_direct_db_source_patch = True  # type: ignore[attr-defined]
        _patched_get_ranking_source_df._original = cur  # type: ignore[attr-defined]
        efr._get_ranking_source_df = _patched_get_ranking_source_df
        _INSTALLED = True
        logger.warning("[RANKING ENTRY DIRECT DB] installed v1")
        return True
    except Exception:
        logger.exception("[RANKING ENTRY DIRECT DB] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY DIRECT DB] auto install failed")


__all__ = ["install"]
