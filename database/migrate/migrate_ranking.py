# ============================================================
# File   : database/migrate/migrate_ranking.py
# Version: Ver37-FIX-RANKING-SUMMARY-SCHEMA-CALL
# ------------------------------------------------------------
# ✔ ADD ONLY 原則厳守
# ✔ Base_ranking create_all保持
# ✔ ranking_raw_1min は database.schema.ranking_raw_schema を正本にする
# ✔ ranking_snapshot_1min は database.schema.ranking_snapshot_schema を正本にする
# ✔ ranking_summary_1min / 3min / 5min を起動時に作成・補完
# ✔ SAFE MIGRATION MODE 対応
# ✔ 既存データ破壊なし
# ✔ raw schema BEGIN IMMEDIATE database is locked を短時間リトライして起動停止を防止
# ✔ migrate_main 互換の migrate_ranking alias を保持
# ✔ Ver37: ensure_ranking_summary_table(con, interval=...) に修正
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from database.bases import Base_ranking
from database.schema.ranking_raw_schema import ensure_ranking_raw_table, get_ranking_raw_schema_status
from database.schema.ranking_snapshot_schema import (
    ensure_ranking_snapshot_table,
    patch_ranking_snapshot_schema,
    ensure_ranking_snapshot_unique_index,
)
from database.schema.ranking_summary_schema import ensure_ranking_summary_table

logger = logging.getLogger(__name__)

LEGACY_RANKING_TYPES: tuple[str, ...] = (
    "値上がり率",
    "値下がり率",
    "売買高上位",
    "売買高急増",
    "売買代金",
    "売買代金急増",
    "TICK回数",
)
LEGACY_MARKETS: tuple[str, ...] = ("ALL", "TP", "TS", "TG")
LEGACY_CATEGORY_COLUMNS: dict[str, str] = {
    "id": "INTEGER",
    "symbol": "TEXT",
    "symbolname": "TEXT",
    "current_price": "REAL",
    "change_percentage": "REAL",
    "change_ratio": "REAL",
    "trading_volume": "REAL",
    "trading_value": "REAL",
    "turnover": "REAL",
    "tick_count": "INTEGER",
    "inserted_at": "TEXT",
    "rank": "INTEGER",
}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _is_sqlite_locked(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "database is locked" in s or "database table is locked" in s or "database busy" in s


def _begin_immediate_with_retry(con: sqlite3.Connection, *, label: str, attempts: int | None = None, sleep_sec: float | None = None) -> bool:
    max_attempts = max(1, attempts if attempts is not None else _env_int("RANKING_MIGRATION_BEGIN_RETRY", 8))
    wait = max(0.1, sleep_sec if sleep_sec is not None else _env_float("RANKING_MIGRATION_BEGIN_RETRY_SLEEP", 0.75))
    for i in range(1, max_attempts + 1):
        try:
            con.execute("BEGIN IMMEDIATE")
            if i > 1:
                logger.warning("[RANKING MIGRATION] BEGIN IMMEDIATE retry success label=%s attempt=%s/%s", label, i, max_attempts)
            return True
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_locked(exc) or i >= max_attempts:
                raise
            logger.warning("[RANKING MIGRATION] BEGIN IMMEDIATE locked label=%s attempt=%s/%s sleep=%.2fs error=%s", label, i, max_attempts, wait, exc)
            time.sleep(wait)
    return False


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _legacy_table_name(ranking_type: str, market: str) -> str:
    rt = str(ranking_type or "").strip() or "UNKNOWN"
    mk = str(market or "ALL").strip() or "ALL"
    return f"{rt}_{mk}"


def _engine_db_path(engine: Any) -> str | None:
    try:
        url = getattr(engine, "url", None)
        db = getattr(url, "database", None)
        if db:
            return str(db)
    except Exception:
        pass
    return None


def _open_sqlite_from_engine(engine: Any) -> sqlite3.Connection | None:
    db_path = _engine_db_path(engine)
    if not db_path:
        logger.warning("[RANKING MIGRATION] sqlite path not resolved from engine")
        return None
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timeout = _env_float("RANKING_MIGRATION_SQLITE_TIMEOUT", 60.0)
    con = sqlite3.connect(str(path), timeout=timeout, check_same_thread=False, isolation_level=None)
    try:
        con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA wal_autocheckpoint=1000")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-50000")
    except Exception:
        logger.warning("[RANKING MIGRATION] sqlite PRAGMA setup partially failed", exc_info=True)
    return con


def _existing_columns_sqlite(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
        return {str(r[1]) for r in rows}
    except Exception:
        logger.exception("[RANKING MIGRATION] read columns failed table=%s", table)
        return set()


def _safe_add_column_sqlite(con: sqlite3.Connection, *, table: str, column: str, col_type: str) -> bool:
    existing = _existing_columns_sqlite(con, table)
    if column in existing:
        return False
    add_type = str(col_type or "TEXT")
    upper = add_type.upper()
    if "DEFAULT CURRENT_TIMESTAMP" in upper:
        add_type = add_type.replace("DEFAULT CURRENT_TIMESTAMP", "").replace("default current_timestamp", "")
    upper = add_type.upper()
    if "NOT NULL" in upper and "DEFAULT" not in upper:
        add_type = add_type.replace("NOT NULL", "").replace("not null", "")
    add_type = " ".join(add_type.split()).strip() or "TEXT"
    try:
        con.execute(f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(column)} {add_type}")
        logger.warning("[RANKING MIGRATION] added missing column table=%s column=%s type=%s", table, column, add_type)
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise


def _ensure_table_and_column(engine, table: str, column: str, col_type: str) -> bool:
    with engine.begin() as conn:
        conn.execute(text("PRAGMA busy_timeout=30000"))
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if table not in tables:
            logger.info("[RANKING MIGRATION] create minimal table table=%s first_column=%s", table, column)
            conn.execute(text(f"CREATE TABLE {_quote_ident(table)} ({_quote_ident(column)} {col_type})"))
            return True
        cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({_quote_ident(table)})"))}
        if column in cols:
            return False
        add_type = str(col_type or "TEXT")
        upper = add_type.upper()
        if "DEFAULT CURRENT_TIMESTAMP" in upper:
            add_type = add_type.replace("DEFAULT CURRENT_TIMESTAMP", "").replace("default current_timestamp", "")
        upper = add_type.upper()
        if "NOT NULL" in upper and "DEFAULT" not in upper:
            add_type = add_type.replace("NOT NULL", "").replace("not null", "")
        add_type = " ".join(add_type.split()).strip() or "TEXT"
        logger.info("[RANKING MIGRATION] add column table=%s column=%s type=%s", table, column, add_type)
        conn.execute(text(f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(column)} {add_type}"))
        return True


def _ensure_ranking_snapshot_schema(engine) -> dict[str, Any]:
    con = _open_sqlite_from_engine(engine)
    if con is None:
        return {"ok": False, "reason": "no_sqlite_connection"}
    try:
        _begin_immediate_with_retry(con, label="snapshot_schema")
        ensure_ranking_snapshot_table(con)
        patch_ranking_snapshot_schema(con)
        unique_ok = False
        try:
            unique_ok = ensure_ranking_snapshot_unique_index(con)
        except Exception:
            logger.warning("[RANKING MIGRATION] snapshot unique index ensure failed", exc_info=True)
        con.execute("COMMIT")
        logger.info("[RANKING MIGRATION] snapshot schema ensured unique_ok=%s", unique_ok)
        return {"ok": True, "unique_ok": unique_ok}
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        logger.exception("[RANKING MIGRATION] snapshot schema ensure failed")
        return {"ok": False, "error": str(e)}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _ensure_ranking_raw_schema(engine) -> dict[str, Any]:
    con = _open_sqlite_from_engine(engine)
    if con is None:
        return {"ok": False, "reason": "no_sqlite_connection"}
    try:
        _begin_immediate_with_retry(con, label="raw_schema")
        ensure_ranking_raw_table(con)
        status = get_ranking_raw_schema_status(con)
        con.execute("COMMIT")
        logger.info("[RANKING MIGRATION] raw schema ensured status=%s", status)
        return {"ok": True, "status": status}
    except sqlite3.OperationalError as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        if _is_sqlite_locked(e):
            logger.warning("[RANKING MIGRATION] raw schema ensure skipped reason=database_locked_after_retry error=%s", e, exc_info=True)
            return {"ok": False, "skip": True, "reason": "database_locked_after_retry", "error": str(e)}
        logger.exception("[RANKING MIGRATION] raw schema ensure failed")
        return {"ok": False, "error": str(e)}
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        logger.exception("[RANKING MIGRATION] raw schema ensure failed")
        return {"ok": False, "error": str(e)}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _ensure_ranking_summary_schemas(engine) -> dict[str, Any]:
    con = _open_sqlite_from_engine(engine)
    if con is None:
        return {"ok": False, "reason": "no_sqlite_connection"}
    try:
        _begin_immediate_with_retry(con, label="summary_schema")
        results: dict[str, Any] = {}
        for interval in (1, 3, 5):
            table = f"ranking_summary_{interval}min"
            ensure_ranking_summary_table(con, interval=interval)
            results[table] = True
        con.execute("COMMIT")
        logger.info("[RANKING MIGRATION] summary schemas ensured results=%s", results)
        return {"ok": True, "results": results}
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        logger.exception("[RANKING MIGRATION] summary schemas ensure failed")
        return {"ok": False, "error": str(e)}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _ensure_legacy_category_tables(engine) -> dict[str, Any]:
    changed = 0
    errors: list[str] = []
    for ranking_type in LEGACY_RANKING_TYPES:
        for market in LEGACY_MARKETS:
            table = _legacy_table_name(ranking_type, market)
            for column, col_type in LEGACY_CATEGORY_COLUMNS.items():
                try:
                    if _ensure_table_and_column(engine, table, column, col_type):
                        changed += 1
                except Exception as exc:
                    msg = f"{table}.{column}: {exc}"
                    errors.append(msg)
                    logger.warning("[RANKING MIGRATION] legacy category ensure failed %s", msg, exc_info=True)
    return {"ok": not errors, "changed": changed, "errors": errors[:20], "error_count": len(errors)}


def run_migration(engine) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "base_create_all": None,
        "snapshot_schema": None,
        "raw_schema": None,
        "summary_schemas": None,
        "legacy_category_tables": None,
    }
    try:
        Base_ranking.metadata.create_all(engine)
        result["base_create_all"] = {"ok": True}
        logger.info("[RANKING MIGRATION] Base_ranking create_all ok")
    except Exception as exc:
        logger.exception("[RANKING MIGRATION] Base_ranking create_all failed")
        result["base_create_all"] = {"ok": False, "error": str(exc)}
        result["ok"] = False

    for key, fn in (
        ("snapshot_schema", _ensure_ranking_snapshot_schema),
        ("raw_schema", _ensure_ranking_raw_schema),
        ("summary_schemas", _ensure_ranking_summary_schemas),
    ):
        try:
            r = fn(engine)
            result[key] = r
            if isinstance(r, dict) and not r.get("ok", False) and not r.get("skip", False):
                result["ok"] = False
        except Exception as exc:
            logger.exception("[RANKING MIGRATION] step failed key=%s", key)
            result[key] = {"ok": False, "error": str(exc)}
            result["ok"] = False

    try:
        r = _ensure_legacy_category_tables(engine)
        result["legacy_category_tables"] = r
        if isinstance(r, dict) and not r.get("ok", False):
            logger.warning("[RANKING MIGRATION] legacy category tables had errors but startup continues result=%s", r)
    except Exception as exc:
        logger.exception("[RANKING MIGRATION] legacy category ensure step failed")
        result["legacy_category_tables"] = {"ok": False, "error": str(exc)}

    logger.info("[RANKING MIGRATION] run_migration done result=%s", result)
    return result


def migrate_ranking(engine) -> dict[str, Any]:
    return run_migration(engine)


__all__ = ["run_migration", "migrate_ranking"]
