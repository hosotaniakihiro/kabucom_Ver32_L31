# ============================================================
# File: database/session.py
# Ver44-NAS-STABLE-MODELS-CANONICAL-SUMMARY-SCHEMA
# ------------------------------------------------------------
# ✔ DB engine / Session 管理
# ✔ SQLite NAS向け busy_timeout / WAL / NullPool 設定
# ✔ summary DB のテーブル作成は database/models.py の ORM 定義を正本にする
# ✔ 既存 summary DB への不足カラム追加も models.py の Base_summary.metadata から自動生成
# ✔ session.py 側の手書き SUMMARY_BOOTSTRAP_COLUMNS を廃止
# ✔ time_range / date / time / start_time / end_time / trading_value / updated_at なども
#   models.py にあれば自動で補完対象になる
# ✔ create_all() は既存テーブルに不足カラムを追加しないため、毎起動 PRAGMA で差分補完
# ✔ SQLite の ALTER TABLE ADD COLUMN では NOT NULL を付けず、安全に列追加する
# ============================================================

from __future__ import annotations

from datetime import date
from pathlib import Path
import logging
import sqlite3
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

# 重要: models import により Base_* metadata に全ORMテーブルを登録する
import database.models  # noqa: F401
from config.paths import get_path
from database.bases import (
    Base_ranking,
    Base_push,
    Base_position,
    Base_summary,
)

logger = logging.getLogger(__name__)


# ============================================================
# GLOBALS
# ============================================================

_push_engine = None
_summary_engine = None
_position_engine = None
_ranking_engine = None
_tosama_engine = None

_Session_push = None
_Session_summary = None
_Session_position = None
_Session_ranking = None
_Session_tosama = None

_initialized = False

push_engine = None
summary_engine = None
position_engine = None
ranking_engine = None
tosama_engine = None


# ============================================================
# SQLITE CONFIG (NAS HARD MODE)
# ============================================================

SQLITE_TIMEOUT_SEC = 60
SQLITE_BUSY_TIMEOUT_MS = 60000
SQLITE_CACHE_SIZE = -50000
SQLITE_SYNCHRONOUS = "NORMAL"
SQLITE_JOURNAL_MODE = "WAL"

ENGINE_KWARGS = dict(
    echo=False,
    future=True,
    poolclass=NullPool,
    connect_args={
        "check_same_thread": False,
        "timeout": SQLITE_TIMEOUT_SEC,
    },
)


# ============================================================
# SQLITE FILE INIT
# ============================================================

def _initialize_sqlite_file(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_SEC)
    cur = conn.cursor()
    try:
        try:
            cur.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE};")
        except Exception:
            logger.warning("[DB INIT] journal_mode init skipped: %s", path)

        try:
            cur.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS};")
        except Exception:
            logger.warning("[DB INIT] synchronous init skipped: %s", path)

        try:
            cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
        except Exception:
            logger.warning("[DB INIT] busy_timeout init skipped: %s", path)

        try:
            cur.execute("PRAGMA temp_store=MEMORY;")
        except Exception:
            logger.warning("[DB INIT] temp_store init skipped: %s", path)

        try:
            cur.execute(f"PRAGMA cache_size={SQLITE_CACHE_SIZE};")
        except Exception:
            logger.warning("[DB INIT] cache_size init skipped: %s", path)

        conn.commit()
    finally:
        cur.close()
        conn.close()


def _best_effort_configure_existing_db(path: Path) -> None:
    try:
        conn = sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_SEC)
        cur = conn.cursor()
        try:
            try:
                cur.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
            except Exception:
                pass

            try:
                cur.execute("PRAGMA temp_store=MEMORY;")
            except Exception:
                pass

            try:
                cur.execute(f"PRAGMA cache_size={SQLITE_CACHE_SIZE};")
            except Exception:
                pass

            try:
                cur.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS};")
            except Exception:
                pass

            try:
                cur.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE};")
            except Exception:
                logger.warning("[DB INIT] existing DB journal_mode skipped: %s", path)

            conn.commit()
        finally:
            cur.close()
            conn.close()
    except Exception:
        logger.warning("[DB INIT] existing DB configuration skipped: %s", path)


# ============================================================
# RUNTIME PRAGMA
# ============================================================

def _attach_runtime_pragmas(engine) -> None:
    from sqlalchemy import event

    if getattr(engine, "_pragma_attached", False):
        return

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001, ARG001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
            cursor.execute("PRAGMA temp_store=MEMORY;")
            cursor.execute(f"PRAGMA cache_size={SQLITE_CACHE_SIZE};")
            cursor.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS};")
            cursor.execute("PRAGMA foreign_keys=ON;")
        finally:
            cursor.close()

    engine._pragma_attached = True


# ============================================================
# UTIL
# ============================================================

def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _today_ymd() -> str:
    return date.today().strftime("%Y%m%d")


def _force_create_sqlite_file(engine) -> None:
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("SELECT 1;")
    except Exception as e:
        logger.error("❌ DB file creation failed: %s", e)
        raise


def _log_engine_info(engine, name: str) -> None:
    logger.info("📂 [%s] %s", name, str(engine.url))


def _ensure_tables(engine, base) -> None:
    tables = list(base.metadata.tables.keys()) if base is not None else []
    if tables:
        base.metadata.create_all(engine)


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(engine, table_name: str) -> bool:
    try:
        with engine.connect() as conn:
            rs = conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name=:table_name"
                ),
                {"table_name": table_name},
            )
            return rs.first() is not None
    except Exception:
        logger.exception("[SCHEMA BOOTSTRAP] table exists check failed table=%s", table_name)
        return False


def _get_table_columns(engine, table_name: str) -> set[str]:
    try:
        if not _table_exists(engine, table_name):
            return set()
        with engine.connect() as conn:
            rs = conn.execute(text(f"PRAGMA table_info({_quote_ident(table_name)})"))
            cols: set[str] = set()
            for row in rs:
                try:
                    cols.add(str(row[1]))
                except Exception:
                    try:
                        cols.add(str(row["name"]))
                    except Exception:
                        pass
            return cols
    except Exception:
        logger.exception("[SCHEMA BOOTSTRAP] PRAGMA table_info failed table=%s", table_name)
        return set()


def _column_exists(engine, table_name: str, column_name: str) -> bool:
    return column_name in _get_table_columns(engine, table_name)


# ============================================================
# SUMMARY SCHEMA BOOTSTRAP FROM database/models.py
# ============================================================

SUMMARY_TARGET_TABLES = (
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
)


def _sqlite_type_from_model_column(column: Any) -> str:
    """SQLAlchemy Column から SQLite ALTER TABLE 用の型文字列を作る。

    既存テーブルへの ALTER TABLE ADD COLUMN では NOT NULL を付けない。
    SQLiteは既存行がある状態で NOT NULL + defaultなし列を追加できないため。
    新規作成時の nullable/制約は models.py の create_all() が担当する。
    """
    try:
        typ = column.type
        name = typ.__class__.__name__.upper()
        if "INTEGER" in name:
            return "INTEGER"
        if "FLOAT" in name or "REAL" in name or "NUMERIC" in name:
            return "REAL"
        if "DATETIME" in name or "DATE" in name or "TIME" in name:
            return "DATETIME"
        if "TEXT" in name:
            return "TEXT"
        if "STRING" in name or "VARCHAR" in name:
            return "VARCHAR"
        compiled = str(typ).upper().strip()
        return compiled or "VARCHAR"
    except Exception:
        return "VARCHAR"


def _summary_model_columns() -> dict[str, list[tuple[str, str]]]:
    """database/models.py の Base_summary.metadata を正本としてカラム一覧を返す。"""
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        metadata = Base_summary.metadata
        for table_name in SUMMARY_TARGET_TABLES:
            tbl = metadata.tables.get(table_name)
            if tbl is None:
                out[table_name] = []
                continue
            cols: list[tuple[str, str]] = []
            for col in tbl.columns:
                col_name = str(col.name)
                if col.primary_key:
                    # id は create_all() 側で作られる。既存DBに後付けしない。
                    continue
                cols.append((col_name, _sqlite_type_from_model_column(col)))
            out[table_name] = cols
    except Exception:
        logger.exception("[SCHEMA BOOTSTRAP] summary model column scan failed")
    return out


def _ensure_column(
    engine,
    table_name: str,
    column_name: str,
    column_type: str,
    existing_columns: set[str] | None = None,
) -> bool:
    """
    Returns:
        True  -> column added
        False -> already existed / table missing / skipped / duplicate race
    """
    try:
        if not _table_exists(engine, table_name):
            logger.info("[SCHEMA BOOTSTRAP] table not found skip table=%s column=%s", table_name, column_name)
            return False

        if existing_columns is None:
            existing_columns = _get_table_columns(engine, table_name)

        if column_name in existing_columns:
            return False

        with engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {_quote_ident(table_name)} "
                    f"ADD COLUMN {_quote_ident(column_name)} {column_type}"
                )
            )

        existing_columns.add(column_name)
        logger.info("[SCHEMA BOOTSTRAP] added table=%s column=%s type=%s", table_name, column_name, column_type)
        return True

    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            try:
                if existing_columns is not None:
                    existing_columns.add(column_name)
            except Exception:
                pass
            logger.debug("[SCHEMA BOOTSTRAP] duplicate column ignored table=%s column=%s", table_name, column_name)
            return False

        logger.exception("[SCHEMA BOOTSTRAP] ensure column failed table=%s column=%s type=%s", table_name, column_name, column_type)
        return False


def _bootstrap_summary_schema(engine) -> None:
    """起動時に summary DB の不足列を database/models.py から補完する。"""
    model_columns = _summary_model_columns()
    targets = tuple(model_columns.keys()) or SUMMARY_TARGET_TABLES

    logger.info("🧱 summary schema bootstrap start targets=%s source=database.models mode=models_canonical", targets)

    added_count = 0
    table_added: dict[str, list[str]] = {}

    for table_name in targets:
        if not _table_exists(engine, table_name):
            logger.warning("[SCHEMA BOOTSTRAP] summary table not found table=%s", table_name)
            continue

        existing_columns = _get_table_columns(engine, table_name)
        table_added[table_name] = []

        for col_name, col_type in model_columns.get(table_name, []):
            if _ensure_column(engine, table_name, col_name, col_type, existing_columns=existing_columns):
                added_count += 1
                table_added[table_name].append(col_name)

        if table_added[table_name]:
            logger.warning("[SCHEMA BOOTSTRAP] table patched table=%s added=%s", table_name, table_added[table_name])
        else:
            logger.info("[SCHEMA BOOTSTRAP] table schema ok table=%s", table_name)

    logger.info("✅ summary schema bootstrap done source=database.models added_columns=%s details=%s", added_count, table_added)


# ============================================================
# ENGINE BUILD
# ============================================================

def _build_engine(db_path: Path, base, name: str):
    _ensure_parent_dir(db_path)

    if not db_path.exists():
        _initialize_sqlite_file(db_path)
    else:
        _best_effort_configure_existing_db(db_path)

    engine = create_engine(f"sqlite:///{db_path}", **ENGINE_KWARGS)
    _attach_runtime_pragmas(engine)

    if base is not None:
        _ensure_tables(engine, base)

    _force_create_sqlite_file(engine)
    _log_engine_info(engine, name)
    return engine


# ============================================================
# INIT
# ============================================================

def init_engines() -> None:
    global _push_engine, _summary_engine
    global _position_engine, _ranking_engine, _tosama_engine
    global _Session_push, _Session_summary
    global _Session_position, _Session_ranking, _Session_tosama
    global _initialized
    global push_engine, summary_engine
    global position_engine, ranking_engine, tosama_engine

    if _initialized:
        return

    logger.info("🚀 INIT ENGINES (NAS STABLE MODELS CANONICAL SUMMARY SCHEMA)")

    today = _today_ymd()

    push_path = get_path("raw_push") / f"push{today}.db"
    _push_engine = _build_engine(push_path, Base_push, "PUSH")
    _Session_push = sessionmaker(bind=_push_engine)

    summary_path = get_path("summary") / f"summary{today}.db"
    _summary_engine = _build_engine(summary_path, Base_summary, "SUMMARY")

    try:
        _bootstrap_summary_schema(_summary_engine)
    except Exception:
        logger.exception("❌ summary schema bootstrap failed")

    _Session_summary = sessionmaker(bind=_summary_engine)

    position_path = get_path("runtime_positions") / "positions.db"
    _position_engine = _build_engine(position_path, Base_position, "POSITION")
    _Session_position = sessionmaker(bind=_position_engine)

    ranking_path = get_path("raw_ranking") / f"ranking{today}.db"
    _ranking_engine = _build_engine(ranking_path, Base_ranking, "RANKING")
    _Session_ranking = sessionmaker(bind=_ranking_engine)

    tosama_path = get_path("ai_data") / f"tosama{today}.db"
    _tosama_engine = _build_engine(tosama_path, None, "TOSAMA")
    _Session_tosama = sessionmaker(bind=_tosama_engine)

    push_engine = _push_engine
    summary_engine = _summary_engine
    position_engine = _position_engine
    ranking_engine = _ranking_engine
    tosama_engine = _tosama_engine

    _initialized = True

    logger.info("✅ ALL ENGINES INITIALIZED (NAS STABLE FINAL)")


def _auto_init() -> None:
    if not _initialized:
        init_engines()


class _SessionProxy:
    def __init__(self, name: str):
        self.name = name

    def __call__(self, *args, **kwargs):
        _auto_init()
        real = globals()[f"_Session_{self.name}"]
        return real(*args, **kwargs)


Session_push = _SessionProxy("push")
Session_summary = _SessionProxy("summary")
Session_position = _SessionProxy("position")
Session_ranking = _SessionProxy("ranking")
Session_tosama = _SessionProxy("tosama")


def get_push_engine():
    _auto_init()
    return _push_engine


def get_summary_engine():
    _auto_init()
    return _summary_engine


def get_position_engine():
    _auto_init()
    return _position_engine


def get_ranking_engine():
    _auto_init()
    return _ranking_engine


def get_tosama_engine():
    _auto_init()
    return _tosama_engine


__all__ = [
    "push_engine",
    "summary_engine",
    "position_engine",
    "ranking_engine",
    "tosama_engine",
    "Session_push",
    "Session_summary",
    "Session_position",
    "Session_ranking",
    "Session_tosama",
    "init_engines",
    "get_push_engine",
    "get_summary_engine",
    "get_position_engine",
    "get_ranking_engine",
    "get_tosama_engine",
]
