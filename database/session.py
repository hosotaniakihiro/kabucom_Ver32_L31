# ============================================================
# File: database/session.py
# Ver43-NAS-ABSOLUTE-STABLE-SUMMARY-WIDE-SCHEMA-EVERY-BOOT
# ------------------------------------------------------------
# ✔ Ver42 全機能保持
# ✔ 起動時に summary table の不足列を毎回 PRAGMA で確認
# ✔ 既存列は skip、無い列だけ ALTER TABLE ADD COLUMN
# ✔ schema_bootstrap_meta の「1回だけskip」を廃止
# ✔ score_buy / score_sell / display_ready / mtf_score / mtf_alignment 追加
# ✔ score_base / score_trend / score_momentum / score_velocity 追加
# ✔ direction_penalty / base_score / momentum_score / volume_score 追加
# ✔ SQLite ADD COLUMN IF NOT EXISTS 非依存
# ✔ duplicate column name は正常競合として握りつぶす
# ✔ production hardened
#
# 【重要】
#   create_all() は既存テーブルに不足カラムを追加しない。
#   そのため _bootstrap_summary_schema() で毎回不足列だけ補完する。
#
# 【今回の修正理由】
#   Ver42 は schema_bootstrap_meta の flag が 1 の場合、
#   あとから追加したカラムがあっても丸ごと skip していた。
#   その結果、score系・ready系・mtf系が DB に存在せず、
#   summary_saver_bulk 側で drop される原因になっていた。
# ============================================================

from __future__ import annotations

from datetime import date
from pathlib import Path
import logging
import sqlite3
from typing import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

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
    tables = list(base.metadata.tables.keys())
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
        logger.exception(
            "[SCHEMA BOOTSTRAP] table exists check failed table=%s",
            table_name,
        )
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
        logger.exception(
            "[SCHEMA BOOTSTRAP] PRAGMA table_info failed table=%s",
            table_name,
        )
        return set()


def _column_exists(engine, table_name: str, column_name: str) -> bool:
    return column_name in _get_table_columns(engine, table_name)


# ============================================================
# SUMMARY SCHEMA BOOTSTRAP
# ============================================================

SUMMARY_TARGET_TABLES = (
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
)

SUMMARY_BOOTSTRAP_COLUMNS = [
    # --------------------------------------------------------
    # basic / metadata
    # --------------------------------------------------------
    ("source", "VARCHAR"),
    ("interval", "INTEGER"),
    ("last_update", "DATETIME"),

    # --------------------------------------------------------
    # OHLC aliases
    # --------------------------------------------------------
    ("open", "FLOAT"),
    ("high", "FLOAT"),
    ("low", "FLOAT"),
    ("close", "FLOAT"),
    ("open_price", "FLOAT"),
    ("high_price", "FLOAT"),
    ("low_price", "FLOAT"),
    ("close_price", "FLOAT"),

    # --------------------------------------------------------
    # volume / MA / technical
    # --------------------------------------------------------
    ("volume", "FLOAT"),
    ("vwap", "FLOAT"),

    ("ma5", "FLOAT"),
    ("ma25", "FLOAT"),
    ("ma75", "FLOAT"),

    ("ma5_conf", "FLOAT"),
    ("ma25_conf", "FLOAT"),
    ("ma75_conf", "FLOAT"),

    ("ma75_slope", "FLOAT"),
    ("volume_slope", "FLOAT"),
    ("vwap_slope", "FLOAT"),

    ("slope", "FLOAT"),
    ("slope_atr_scaled", "FLOAT"),
    ("slope_atr_scaled_1m", "FLOAT"),
    ("slope_atr_scaled_3m", "FLOAT"),
    ("slope_atr_scaled_5m", "FLOAT"),

    ("ema12", "FLOAT"),
    ("ema26", "FLOAT"),
    ("macd", "FLOAT"),
    ("signal", "FLOAT"),
    ("hist", "FLOAT"),

    ("rsi", "FLOAT"),
    ("rci", "FLOAT"),

    ("atr", "FLOAT"),
    ("atr_1m", "FLOAT"),
    ("atr_3m", "FLOAT"),
    ("atr_5m", "FLOAT"),

    ("bb_mid", "FLOAT"),
    ("bb_upper", "FLOAT"),
    ("bb_lower", "FLOAT"),
    ("bb_width", "FLOAT"),

    # --------------------------------------------------------
    # score / display
    # --------------------------------------------------------
    ("score", "FLOAT"),
    ("score_total", "FLOAT"),
    ("display_score", "FLOAT"),
    ("final_score", "FLOAT"),

    ("score_buy", "FLOAT"),
    ("score_sell", "FLOAT"),
    ("buy_score", "FLOAT"),
    ("sell_score", "FLOAT"),

    ("score_slope", "FLOAT"),
    ("score_mtf", "FLOAT"),

    ("mtf", "FLOAT"),
    ("mtf_alignment", "FLOAT"),
    ("mtf_score", "FLOAT"),

    ("price_diff", "FLOAT"),

    ("base", "FLOAT"),
    ("trend", "FLOAT"),
    ("mom", "FLOAT"),
    ("vel", "FLOAT"),
    ("pen", "FLOAT"),

    ("combined_score", "FLOAT"),

    # --------------------------------------------------------
    # score alias / diagnostics
    # --------------------------------------------------------
    ("score_base", "FLOAT"),
    ("score_trend", "FLOAT"),
    ("score_momentum", "FLOAT"),
    ("score_velocity", "FLOAT"),
    ("direction_penalty", "FLOAT"),

    ("base_score", "FLOAT"),
    ("momentum_score", "FLOAT"),
    ("volume_score", "FLOAT"),
    ("flag_score", "FLOAT"),

    ("sell_pressure", "FLOAT"),
    ("absolute_score", "FLOAT"),
    ("liquidity_score", "FLOAT"),
    ("distribution_score", "FLOAT"),
    ("volatility_score", "FLOAT"),
    ("score_rank", "FLOAT"),
    ("ai_score", "FLOAT"),

    # --------------------------------------------------------
    # readiness / history
    # --------------------------------------------------------
    ("symbol_hist_len", "FLOAT"),
    ("technical_ready", "INTEGER"),
    ("display_ready", "INTEGER"),
]


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
            logger.info(
                "[SCHEMA BOOTSTRAP] table not found skip table=%s column=%s",
                table_name,
                column_name,
            )
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

        logger.info(
            "[SCHEMA BOOTSTRAP] added table=%s column=%s type=%s",
            table_name,
            column_name,
            column_type,
        )
        return True

    except Exception as e:
        msg = str(e).lower()

        # 複数スレッド/再起動競合で先に追加された場合は正常扱い
        if "duplicate column" in msg or "already exists" in msg:
            try:
                if existing_columns is not None:
                    existing_columns.add(column_name)
            except Exception:
                pass
            logger.debug(
                "[SCHEMA BOOTSTRAP] duplicate column ignored table=%s column=%s",
                table_name,
                column_name,
            )
            return False

        logger.exception(
            "[SCHEMA BOOTSTRAP] ensure column failed table=%s column=%s type=%s",
            table_name,
            column_name,
            column_type,
        )
        return False


def _bootstrap_summary_schema(engine) -> None:
    """
    起動時に summary DB の不足列を補完する。

    Ver43:
      - meta flag による丸ごと skip はしない
      - 毎回 PRAGMA table_info を見て、無い列だけ追加する
      - 既存列は無音 skip
      - duplicate column は競合として無視
    """
    targets = SUMMARY_TARGET_TABLES

    logger.info("🧱 summary schema bootstrap start targets=%s mode=every_boot_missing_only", targets)

    added_count = 0
    table_added: dict[str, list[str]] = {}

    for table_name in targets:
        if not _table_exists(engine, table_name):
            logger.warning("[SCHEMA BOOTSTRAP] summary table not found table=%s", table_name)
            continue

        existing_columns = _get_table_columns(engine, table_name)
        table_added[table_name] = []

        for col_name, col_type in SUMMARY_BOOTSTRAP_COLUMNS:
            if _ensure_column(
                engine,
                table_name,
                col_name,
                col_type,
                existing_columns=existing_columns,
            ):
                added_count += 1
                table_added[table_name].append(col_name)

        if table_added[table_name]:
            logger.warning(
                "[SCHEMA BOOTSTRAP] table patched table=%s added=%s",
                table_name,
                table_added[table_name],
            )
        else:
            logger.info("[SCHEMA BOOTSTRAP] table schema ok table=%s", table_name)

    logger.info(
        "✅ summary schema bootstrap done added_columns=%s details=%s",
        added_count,
        table_added,
    )


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

    logger.info("🚀 INIT ENGINES (NAS ABSOLUTE STABLE SUMMARY WIDE SCHEMA EVERY BOOT MODE)")

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

    logger.info("✅ ALL ENGINES INITIALIZED (NAS ABSOLUTE STABLE FINAL)")


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