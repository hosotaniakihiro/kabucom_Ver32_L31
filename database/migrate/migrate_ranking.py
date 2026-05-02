# ============================================================
# File   : database/migrate/migrate_ranking.py
# Version: Ver34-STARTUP-CREATE-ALL-RANKING-DB-TABLES
# ------------------------------------------------------------
# ✔ ADD ONLY 原則厳守
# ✔ Base_ranking create_all保持
# ✔ ranking_raw_1min は database.schema.ranking_raw_schema を正本にする
# ✔ ranking_snapshot_1min は database.schema.ranking_snapshot_schema を正本にする
# ✔ ranking_ma_1min 列保証
# ✔ ranking_summary_1min / 3min / 5min を起動時に作成・補完
# ✔ 旧カテゴリ別テーブル（値上がり率_ALL 等）を起動時に作成・補完
# ✔ SAFE MIGRATION MODE 対応
# ✔ 既存データ破壊なし
# ✔ writer側からスキーマ責務を移管
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import text

from database.bases import Base_ranking
from database.schema.ranking_raw_schema import (
    ensure_ranking_raw_table,
    get_ranking_raw_schema_status,
)
from database.schema.ranking_snapshot_schema import (
    ensure_ranking_snapshot_table,
    patch_ranking_snapshot_schema,
    ensure_ranking_snapshot_unique_index,
)
from database.schema.ranking_summary_schema import ensure_ranking_summary_table

logger = logging.getLogger(__name__)


# ============================================================
# constants
# ============================================================

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


# ============================================================
# helpers
# ============================================================

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

    con = sqlite3.connect(
        str(path),
        timeout=60,
        check_same_thread=False,
        isolation_level=None,
    )

    try:
        con.execute("PRAGMA busy_timeout=60000")
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


def _safe_add_column_sqlite(
    con: sqlite3.Connection,
    *,
    table: str,
    column: str,
    col_type: str,
) -> bool:
    existing = _existing_columns_sqlite(con, table)
    if column in existing:
        return False

    add_type = str(col_type or "TEXT")
    upper = add_type.upper()

    # SQLite の ALTER TABLE ADD COLUMN では DEFAULT CURRENT_TIMESTAMP や
    # NOT NULL without DEFAULT が古いDBで失敗しやすいため除去する。
    if "DEFAULT CURRENT_TIMESTAMP" in upper:
        add_type = add_type.replace("DEFAULT CURRENT_TIMESTAMP", "")
        add_type = add_type.replace("default current_timestamp", "")

    upper = add_type.upper()
    if "NOT NULL" in upper and "DEFAULT" not in upper:
        add_type = add_type.replace("NOT NULL", "")
        add_type = add_type.replace("not null", "")

    add_type = " ".join(add_type.split()).strip() or "TEXT"

    try:
        con.execute(
            f"ALTER TABLE {_quote_ident(table)} "
            f"ADD COLUMN {_quote_ident(column)} {add_type}"
        )
        logger.warning(
            "[RANKING MIGRATION] added missing column table=%s column=%s type=%s",
            table,
            column,
            add_type,
        )
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise


def _ensure_table_and_column(engine, table: str, column: str, col_type: str) -> bool:
    """
    SQLAlchemy engine 経由で ADD ONLY の列保証を行う。
    """
    with engine.begin() as conn:
        conn.execute(text("PRAGMA busy_timeout=30000"))

        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }

        if table not in tables:
            logger.info("[RANKING MIGRATION] create minimal table table=%s first_column=%s", table, column)
            conn.execute(text(f"CREATE TABLE {_quote_ident(table)} ({_quote_ident(column)} {col_type})"))
            return True

        cols = {
            row[1]
            for row in conn.execute(
                text(f"PRAGMA table_info({_quote_ident(table)})")
            )
        }

        if column in cols:
            return False

        add_type = str(col_type or "TEXT")

        upper = add_type.upper()
        if "DEFAULT CURRENT_TIMESTAMP" in upper:
            add_type = add_type.replace("DEFAULT CURRENT_TIMESTAMP", "")
            add_type = add_type.replace("default current_timestamp", "")

        upper = add_type.upper()
        if "NOT NULL" in upper and "DEFAULT" not in upper:
            add_type = add_type.replace("NOT NULL", "")
            add_type = add_type.replace("not null", "")

        add_type = " ".join(add_type.split()).strip() or "TEXT"

        logger.info("[RANKING MIGRATION] add column table=%s column=%s type=%s", table, column, add_type)
        conn.execute(
            text(
                f"ALTER TABLE {_quote_ident(table)} "
                f"ADD COLUMN {_quote_ident(column)} {add_type}"
            )
        )
        return True


# ============================================================
# ensure schemas
# ============================================================

def _ensure_ranking_snapshot_schema(engine) -> dict[str, Any]:
    """
    snapshot schema は database.schema.ranking_snapshot_schema を正本にする。
    """
    con = _open_sqlite_from_engine(engine)
    if con is None:
        return {"ok": False, "reason": "no_sqlite_connection"}

    try:
        con.execute("BEGIN IMMEDIATE")
        ensure_ranking_snapshot_table(con)
        patch_ranking_snapshot_schema(con)

        unique_ok = False
        try:
            ensure_ranking_snapshot_unique_index(con)
            unique_ok = True
        except Exception:
            # 既存重複データや旧スキーマで unique index が張れない場合でも、
            # writer は DELETE -> INSERT 方式なので起動は止めない。
            logger.warning(
                "[RANKING MIGRATION] snapshot unique index ensure skipped/failed",
                exc_info=True,
            )

        con.execute("COMMIT")

        logger.info(
            "[RANKING MIGRATION] snapshot schema ensured unique_ok=%s",
            unique_ok,
        )

        return {
            "ok": True,
            "unique_ok": unique_ok,
        }

    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass

        logger.exception("[RANKING MIGRATION] snapshot schema ensure failed")
        return {
            "ok": False,
            "error": str(e),
        }

    finally:
        try:
            con.close()
        except Exception:
            pass


def _ensure_ranking_raw_schema(engine) -> dict[str, Any]:
    """
    raw schema は database.schema.ranking_raw_schema を正本にする。
    """
    con = _open_sqlite_from_engine(engine)
    if con is None:
        return {"ok": False, "reason": "no_sqlite_connection"}

    try:
        con.execute("BEGIN IMMEDIATE")
        ensure_ranking_raw_table(con)
        status = get_ranking_raw_schema_status(con)
        con.execute("COMMIT")

        logger.info("[RANKING MIGRATION] raw schema ensured status=%s", status)

        return {
            "ok": True,
            "status": status,
        }

    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass

        logger.exception("[RANKING MIGRATION] raw schema ensure failed")
        return {
            "ok": False,
            "error": str(e),
        }

    finally:
        try:
            con.close()
        except Exception:
            pass


def _ensure_ranking_summary_schemas(engine) -> dict[str, Any]:
    """
    ranking_summary_1min / 3min / 5min を起動時に作成・補完する。
    """
    con = _open_sqlite_from_engine(engine)
    if con is None:
        return {"ok": False, "reason": "no_sqlite_connection"}

    ensured: list[str] = []

    try:
        con.execute("BEGIN IMMEDIATE")
        for interval in (1, 3, 5):
            ensure_ranking_summary_table(con, interval=interval)
            ensured.append(f"ranking_summary_{interval}min")
        con.execute("COMMIT")

        logger.info("[RANKING MIGRATION] ranking summary schemas ensured tables=%s", ensured)
        return {"ok": True, "tables": ensured}

    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        logger.exception("[RANKING MIGRATION] ranking summary schemas ensure failed")
        return {"ok": False, "error": str(e), "tables": ensured}

    finally:
        try:
            con.close()
        except Exception:
            pass


def _ensure_legacy_category_table(con: sqlite3.Connection, table: str) -> int:
    q = _quote_ident(table)
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {q} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            symbolname TEXT,
            current_price REAL,
            change_percentage REAL,
            change_ratio REAL,
            trading_volume REAL,
            trading_value REAL,
            turnover REAL,
            tick_count INTEGER,
            inserted_at TEXT,
            rank INTEGER
        )
        """
    )

    added = 0
    for col, typ in LEGACY_CATEGORY_COLUMNS.items():
        if _safe_add_column_sqlite(con, table=table, column=col, col_type=typ):
            added += 1

    con.execute(
        f"CREATE INDEX IF NOT EXISTS {_quote_ident('idx_' + table + '_inserted_at')} "
        f"ON {q}(inserted_at)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS {_quote_ident('idx_' + table + '_symbol_inserted_at')} "
        f"ON {q}(symbol, inserted_at)"
    )

    return added


def _ensure_legacy_category_schemas(engine) -> dict[str, Any]:
    """
    値上がり率_ALL など旧カテゴリ別ランキングテーブルを起動時に作成・補完する。
    """
    con = _open_sqlite_from_engine(engine)
    if con is None:
        return {"ok": False, "reason": "no_sqlite_connection"}

    tables: list[str] = []
    added_columns: dict[str, int] = {}

    try:
        con.execute("BEGIN IMMEDIATE")

        for ranking_type in LEGACY_RANKING_TYPES:
            for market in LEGACY_MARKETS:
                table = _legacy_table_name(ranking_type, market)
                added = _ensure_legacy_category_table(con, table)
                tables.append(table)
                if added:
                    added_columns[table] = added

        con.execute("COMMIT")

        logger.info(
            "[RANKING MIGRATION] legacy category schemas ensured tables=%d added_columns=%s",
            len(tables),
            added_columns,
        )

        return {
            "ok": True,
            "tables": tables,
            "added_columns": added_columns,
        }

    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass

        logger.exception("[RANKING MIGRATION] legacy category schemas ensure failed")
        return {
            "ok": False,
            "error": str(e),
            "tables": tables,
            "added_columns": added_columns,
        }

    finally:
        try:
            con.close()
        except Exception:
            pass


def _ensure_ranking_ma_schema(engine) -> dict[str, Any]:
    """
    ranking_ma_1min は従来通り migration 側で列保証する。
    """
    ranking_ma_columns = {
        "id": "INTEGER",
        "symbol": "TEXT",
        "rank_type": "TEXT",
        "market": "TEXT",
        "ma_rank_position": "REAL",
        "ma_volume_speed": "REAL",
        "trend_score": "REAL",
        "snapshot_time": "TEXT",
        "created_at": "TEXT",
    }

    added: list[str] = []

    try:
        for col, typ in ranking_ma_columns.items():
            if _ensure_table_and_column(engine, "ranking_ma_1min", col, typ):
                added.append(col)

        logger.info("[RANKING MIGRATION] ranking_ma_1min ensured added=%s", added)

        return {
            "ok": True,
            "added": added,
        }

    except Exception as e:
        logger.exception("[RANKING MIGRATION] ranking_ma_1min ensure failed")
        return {
            "ok": False,
            "error": str(e),
            "added": added,
        }


# ============================================================
# public
# ============================================================

def migrate_ranking(engine) -> dict[str, Any]:
    """
    Ranking DB migration.

    SAFE MIGRATION MODE:
      - 既存データ削除なし
      - ADD COLUMN のみ
      - snapshot/raw schema は database.schema 側の正本を利用
      - summary / legacy category tables も起動時に作成済みにする
    """
    logger.info("[RANKING MIGRATION] start version=Ver34-STARTUP-CREATE-ALL")

    result: dict[str, Any] = {
        "ok": False,
        "base_create_all": False,
        "raw": {},
        "snapshot": {},
        "ma": {},
        "summary": {},
        "legacy": {},
    }

    try:
        Base_ranking.metadata.create_all(engine)
        result["base_create_all"] = True
        logger.info("[RANKING MIGRATION] Base_ranking create_all done")
    except Exception as e:
        logger.exception("[RANKING MIGRATION] Base_ranking create_all failed")
        result["base_create_all_error"] = str(e)

    result["raw"] = _ensure_ranking_raw_schema(engine)
    result["snapshot"] = _ensure_ranking_snapshot_schema(engine)
    result["ma"] = _ensure_ranking_ma_schema(engine)
    result["summary"] = _ensure_ranking_summary_schemas(engine)
    result["legacy"] = _ensure_legacy_category_schemas(engine)

    result["ok"] = bool(
        result.get("base_create_all")
        and result.get("raw", {}).get("ok")
        and result.get("snapshot", {}).get("ok")
        and result.get("ma", {}).get("ok")
        and result.get("summary", {}).get("ok")
        and result.get("legacy", {}).get("ok")
    )

    logger.info("[RANKING MIGRATION] complete result=%s", result)

    return result


__all__ = [
    "migrate_ranking",
]
