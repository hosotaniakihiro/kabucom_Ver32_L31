# ============================================================
# File   : core/startup/yahoo_direct_summary_upsert_conflict_patch.py
# Version: V1-YAHOO-DIRECT-SUMMARY-ON-CONFLICT
# ------------------------------------------------------------
# Purpose:
#   Yahoo recovery direct SQLite fallback can fail with:
#     UNIQUE constraint failed: stock_summary_3min.symbol, stock_summary_3min.datetime
#   when its DELETE key differs from the actual unique index.
#
# Fix:
#   - Detect actual UNIQUE index columns from SQLite PRAGMA index_list/index_info.
#   - Prefer symbol+datetime when present.
#   - Deduplicate by the actual conflict key.
#   - Use INSERT ... ON CONFLICT(key) DO UPDATE instead of DELETE -> INSERT.
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_DIRECT = None


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _unique_indexes(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    out: list[list[str]] = []
    try:
        for row in conn.execute(f"PRAGMA index_list({_quote(table)})").fetchall():
            # row: seq, name, unique, origin, partial
            name = str(row[1])
            unique = int(row[2] or 0)
            if unique != 1:
                continue
            cols = [str(r[2]) for r in conn.execute(f"PRAGMA index_info({_quote(name)})").fetchall() if r[2] is not None]
            if cols:
                out.append(cols)
    except Exception:
        logger.debug("[YAHOO DIRECT UPSERT CONFLICT PATCH] unique index inspect failed table=%s", table, exc_info=True)
    return out


def _choose_key_cols(conn: sqlite3.Connection, table: str, work: pd.DataFrame, table_cols: list[str], interval: int, save_mod: Any) -> list[str]:
    cols = set(work.columns)
    table_set = set(table_cols)
    uniques = _unique_indexes(conn, table)

    preferred = [
        ["symbol", "datetime"],
        ["symbol", "date", "time_range"],
        ["symbol", "date", "time"],
    ]
    for pref in preferred:
        if set(pref).issubset(cols) and set(pref).issubset(table_set):
            for u in uniques:
                if list(u) == pref:
                    return pref

    for u in uniques:
        if set(u).issubset(cols) and set(u).issubset(table_set):
            return list(u)

    try:
        key = save_mod._key_columns_for_table(work, interval=interval, table_cols=table_cols)
        if key and set(key).issubset(cols) and set(key).issubset(table_set):
            return list(key)
    except Exception:
        pass
    return []


def _to_records(save_mod: Any, work: pd.DataFrame, cols: list[str]) -> list[tuple[Any, ...]]:
    return [tuple(save_mod._to_sqlite_value(row.get(c)) for c in cols) for _, row in work[cols].iterrows()]


def install() -> bool:
    global _INSTALLED, _ORIG_DIRECT
    if _INSTALLED:
        return True
    try:
        import trading.yahoo.pipeline.complement.save as save_mod
    except Exception:
        logger.exception("[YAHOO DIRECT UPSERT CONFLICT PATCH] import failed")
        return False

    cur = getattr(save_mod, "_direct_sqlite_upsert_summary_df", None)
    if not callable(cur):
        logger.warning("[YAHOO DIRECT UPSERT CONFLICT PATCH] target missing")
        return False
    if getattr(cur, "_yahoo_direct_conflict_patch_v1", False):
        _INSTALLED = True
        return True

    _ORIG_DIRECT = cur

    def _patched_direct_sqlite_upsert_summary_df(df: pd.DataFrame, *, interval: int, db_path: Optional[str] = None) -> int:
        out = save_mod.safe_df(df)
        if out.empty:
            return 0

        table = save_mod.summary_table_for_interval(interval)
        db_path2 = db_path or save_mod.get_summary_db_path(date_yyyymmdd=save_mod._detect_date_yyyymmdd(out))
        con: sqlite3.Connection | None = None
        try:
            if not os.path.exists(str(db_path2)):
                logger.error("[YAHOO SAVE][DIRECT CONFLICT] summary db not found interval=%s table=%s db=%s", interval, table, db_path2)
                return 0

            timeout = max(float(getattr(save_mod, "YAHOO_SUMMARY_LOCK_TIMEOUT_SEC", 15.0)), 15.0)
            con = sqlite3.connect(str(db_path2), timeout=timeout)
            con.execute("PRAGMA busy_timeout = 15000")
            try:
                con.execute("PRAGMA journal_mode = WAL")
                con.execute("PRAGMA synchronous = NORMAL")
            except Exception:
                pass

            exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists is None:
                logger.error("[YAHOO SAVE][DIRECT CONFLICT] summary table not found interval=%s table=%s db=%s", interval, table, db_path2)
                return 0

            table_cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({_quote(table)})").fetchall()]
            work = save_mod._normalize_for_sqlite(out)

            if "last_update" in table_cols and "last_update" not in work.columns:
                work["last_update"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
            if "source" in table_cols and "source" not in work.columns:
                work["source"] = "summary_recovery_yahoo_1m" if int(interval) == 1 else f"summary_recovery_yahoo_resample_{int(interval)}m"

            cols = [c for c in table_cols if c in work.columns and c != "id"]
            if not cols:
                logger.error("[YAHOO SAVE][DIRECT CONFLICT] no matching columns interval=%s table=%s", interval, table)
                return 0

            key_cols = _choose_key_cols(con, table, work, table_cols, interval, save_mod)
            if not key_cols:
                logger.warning("[YAHOO SAVE][DIRECT CONFLICT] no conflict key -> original fallback interval=%s table=%s", interval, table)
                return int(_ORIG_DIRECT(out, interval=interval, db_path=db_path2) or 0)

            before = len(work)
            work = work.dropna(subset=key_cols).drop_duplicates(subset=key_cols, keep="last").reset_index(drop=True)
            if work.empty:
                logger.warning("[YAHOO SAVE][DIRECT CONFLICT] no rows after key cleanup interval=%s table=%s key=%s", interval, table, key_cols)
                return 0

            placeholders = ",".join(["?"] * len(cols))
            col_sql = ",".join(_quote(c) for c in cols)
            conflict_sql = ",".join(_quote(c) for c in key_cols)
            update_cols = [c for c in cols if c not in key_cols]
            if update_cols:
                update_sql = ",".join([f"{_quote(c)}=excluded.{_quote(c)}" for c in update_cols])
                insert_sql = f"INSERT INTO {_quote(table)} ({col_sql}) VALUES ({placeholders}) ON CONFLICT({conflict_sql}) DO UPDATE SET {update_sql}"
            else:
                insert_sql = f"INSERT INTO {_quote(table)} ({col_sql}) VALUES ({placeholders}) ON CONFLICT({conflict_sql}) DO NOTHING"

            records = _to_records(save_mod, work, cols)
            con.executemany(insert_sql, records)
            con.commit()
            logger.info(
                "[YAHOO SAVE][DIRECT CONFLICT] summary sqlite upsert done interval=%s table=%s rows=%s before=%s dropped_dup=%s key=%s db=%s source=%s latest=%s cols=%s",
                interval,
                table,
                len(records),
                before,
                before - len(work),
                key_cols,
                db_path2,
                (work["source"].iloc[0] if "source" in work.columns and not work.empty else None),
                (work["datetime"].max() if "datetime" in work.columns and not work.empty else None),
                len(cols),
            )
            return int(len(records))

        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                logger.warning("[YAHOO SAVE][DIRECT CONFLICT] database busy/locked interval=%s table=%s db=%s err=%s", interval, table, db_path2, e)
            else:
                logger.exception("[YAHOO SAVE][DIRECT CONFLICT] sqlite operational error interval=%s table=%s db=%s", interval, table, db_path2)
            if con is not None:
                try:
                    con.rollback()
                except Exception:
                    pass
            return 0
        except Exception:
            if con is not None:
                try:
                    con.rollback()
                except Exception:
                    pass
            logger.exception("[YAHOO SAVE][DIRECT CONFLICT] direct sqlite upsert failed interval=%s table=%s db=%s", interval, table, db_path2)
            return 0
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

    _patched_direct_sqlite_upsert_summary_df._yahoo_direct_conflict_patch_v1 = True  # type: ignore[attr-defined]
    save_mod._direct_sqlite_upsert_summary_df = _patched_direct_sqlite_upsert_summary_df
    _INSTALLED = True
    logger.warning("[YAHOO DIRECT UPSERT CONFLICT PATCH] installed v1 on_conflict=1")
    return True


try:
    install()
except Exception:
    logger.exception("[YAHOO DIRECT UPSERT CONFLICT PATCH] auto install failed")

__all__ = ["install"]
