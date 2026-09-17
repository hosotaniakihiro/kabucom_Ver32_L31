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
import os
import sqlite3
import threading
import time
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

# 旧 fast_startup_runtime_patch が _bootstrap_summary_schema を no-op へ
# 差し替えていたのと同じ効果。apply_fast_startup_schema_skip() で True になる。
_SKIP_SUMMARY_SCHEMA_BOOTSTRAP = False

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

def _conn_cache_size():
    """接続ごとの cache_size。既定では設定しない (None)。

    ENGINE_KWARGS は poolclass=NullPool なので、接続はクエリごとに作られて
    すぐ捨てられる。そこで cache_size=-50000 (=50MB) を確保しても
    一度も再利用されないまま解放される。
    NAS(SMB) 越しではこれが MaxCmds=50 を飽和させ、
    sqlite3.OperationalError: disk I/O error の直接原因になっていた
    (2026-08-26 のログで disk I/O error 100 件中 90 件がこの行)。
    """
    v = os.getenv("SQLITE_CONN_CACHE_SIZE")
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(str(v).strip()))
    except Exception:
        logger.warning("[DB PRAGMA] SQLITE_CONN_CACHE_SIZE を解釈できない: %r", v)
        return None


def _pragma_failsafe() -> bool:
    """重要 PRAGMA の失敗時に接続を捨てるか。既定 True (握り潰さない)。"""
    v = os.getenv("SQLITE_PRAGMA_FAILSAFE")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _pragma_strict_all() -> bool:
    """全 PRAGMA を重要扱いにするか (以前の挙動)。既定 False。"""
    v = os.getenv("SQLITE_PRAGMA_STRICT_ALL")
    if v is None or str(v).strip() == "":
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


# データ整合性に効く PRAGMA だけを「重要」とする。
# 他は性能・耐久性のチューニングで、失敗しても接続自体は使える。
# 失敗のたびに接続を捨てて再接続すると、NAS 越しでは
# 毎分 100 件超のリトライストームになり fd を食い潰す (2026-08-27 実測)。
_CRITICAL_PRAGMAS = frozenset({"foreign_keys"})


def _pragma_is_critical(name: str) -> bool:
    if _pragma_strict_all():
        return True
    return str(name or "") in _CRITICAL_PRAGMAS


def _pragma_log_first() -> int:
    try:
        return max(1, int(float(os.getenv("SQLITE_PRAGMA_LOG_FIRST", "5"))))
    except Exception:
        return 5


def _pragma_log_every() -> int:
    try:
        return max(1, int(float(os.getenv("SQLITE_PRAGMA_LOG_EVERY", "200"))))
    except Exception:
        return 200


_PRAGMA_FAIL_COUNTS: dict = {}
_PRAGMA_FAIL_LOCK = threading.Lock()


def _pragma_should_log(db_path: str, pragma: str) -> tuple:
    """(出すか, 累計件数) を返す。

    同じ (db, pragma) の ERROR を 3,355 行も NAS 上のログへ書くこと自体が
    NAS の負荷になっていたため間引く。ただし累計件数は必ず添える。
    """
    key = (str(db_path), str(pragma))
    with _PRAGMA_FAIL_LOCK:
        n = _PRAGMA_FAIL_COUNTS.get(key, 0) + 1
        _PRAGMA_FAIL_COUNTS[key] = n
    if n <= _pragma_log_first():
        return True, n
    if n % _pragma_log_every() == 0:
        return True, n
    return False, n


def pragma_failure_snapshot() -> dict:
    """診断用: (db, pragma) ごとの失敗累計。"""
    with _PRAGMA_FAIL_LOCK:
        return {"%s|%s" % k: v for k, v in _PRAGMA_FAIL_COUNTS.items()}


def mark_engine_readonly(engine) -> None:
    """このエンジンを読み取り専用として扱う。

    読み取り接続に synchronous / foreign_keys を投げても意味が無く、
    NAS 越しでは失敗するだけコストになる。
    """
    try:
        engine._sqlite_readonly = True
    except Exception:
        logger.debug("[DB PRAGMA] mark_engine_readonly failed", exc_info=True)


# ============================================================
# P0-6: 接続の実数を数える
# ------------------------------------------------------------
# NullPool なのでクエリごとに接続が作られる。SQLAlchemy の pool
# メトリクス (checkedin/checkedout) は NullPool では意味を持たないため、
# connect / close イベントを直接数えて「今 NAS へ何本開いているか」を出す。
# outstanding が単調増加したら接続リーク。
# ============================================================

_ENGINE_CONN_STATS: dict = {}
_ENGINE_CONN_LOCK = threading.Lock()


def _conn_stat_bump(db_path: str, key: str) -> None:
    with _ENGINE_CONN_LOCK:
        d = _ENGINE_CONN_STATS.setdefault(db_path, {"opened": 0, "closed": 0, "peak_outstanding": 0})
        d[key] = d.get(key, 0) + 1
        out = d["opened"] - d["closed"]
        if out > d["peak_outstanding"]:
            d["peak_outstanding"] = out


def connection_stats() -> dict:
    """DB ごとの接続 opened / closed / outstanding / peak。"""
    with _ENGINE_CONN_LOCK:
        out = {}
        for path, d in _ENGINE_CONN_STATS.items():
            name = str(path).rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or path
            out[name] = {
                "opened": d.get("opened", 0),
                "closed": d.get("closed", 0),
                "outstanding": d.get("opened", 0) - d.get("closed", 0),
                "peak_outstanding": d.get("peak_outstanding", 0),
            }
        return out


def _attach_runtime_pragmas(engine) -> None:
    from sqlalchemy import event

    if getattr(engine, "_pragma_attached", False):
        return

    db_path = str(getattr(getattr(engine, "url", None), "database", "") or "")

    @event.listens_for(engine, "close")
    def _on_close(dbapi_connection, connection_record):  # noqa: ANN001, ARG001
        _conn_stat_bump(db_path, "closed")

    @event.listens_for(engine, "close_detached")
    def _on_close_detached(dbapi_connection):  # noqa: ANN001
        _conn_stat_bump(db_path, "closed")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001, ARG001
        _conn_stat_bump(db_path, "opened")
        t0 = time.perf_counter()
        cursor = dbapi_connection.cursor()
        readonly = bool(getattr(engine, "_sqlite_readonly", False))

        # 永続するのは journal_mode(WAL) だけ。
        # synchronous / foreign_keys は接続ごとの設定で、
        # 設定しないと FULL / OFF に戻る (実測確認済み)。
        # ただし読み取り専用エンジンには書き込み系を投げない。
        plan = [("busy_timeout", f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};"),
                ("temp_store", "PRAGMA temp_store=MEMORY;")]
        if not readonly:
            plan.append(("synchronous", f"PRAGMA synchronous={SQLITE_SYNCHRONOUS};"))
            plan.append(("foreign_keys", "PRAGMA foreign_keys=ON;"))
        # cache_size だけは既定で設定しない。
        # NullPool では接続が使い捨てなので 50MB のページキャッシュは
        # 一度も再利用されず、SMB(MaxCmds=50) を飽和させて
        # disk I/O error の直接原因になっていた。
        cs = _conn_cache_size()
        if cs is not None:
            plan.append(("cache_size", f"PRAGMA cache_size={int(cs)};"))

        try:
            for pragma, sql in plan:
                try:
                    cursor.execute(sql)
                except Exception as e:
                    critical = _pragma_is_critical(pragma)
                    should_log, total = _pragma_should_log(db_path, pragma)
                    if should_log:
                        # 握り潰さない。DB 名・pragma・thread・elapsed・例外を必ず出す。
                        logger.error(
                            "[DB PRAGMA] failed db=%s pragma=%s thread=%s conn_id=%s "
                            "elapsed=%.3fs critical=%s fail_count=%d readonly=%s err=%r",
                            db_path or "<unknown>", pragma,
                            threading.current_thread().name, id(dbapi_connection),
                            time.perf_counter() - t0, critical, total, readonly, e,
                        )
                        # P0-6: 原因を推測しないため、直前 5 秒の資源状況を必ず添える。
                        # disk I/O error は positions.db 固有ではなく、
                        # プロセス全体のハンドル枯渇の症状として出ることがある。
                        try:
                            from core.diagnostics.resource_monitor import (
                                log_io_error_chronology,
                            )

                            log_io_error_chronology(
                                "DB PRAGMA %s db=%s" % (pragma, db_path or "<unknown>"), e
                            )
                        except Exception:
                            logger.debug("[DB PRAGMA] chronology unavailable", exc_info=True)
                    if critical and _pragma_failsafe():
                        # データ整合性に関わる PRAGMA が効いていない接続は使わせない
                        raise
                    # 非重要 PRAGMA は接続を捨てない。
                    # 捨てて再接続を繰り返すと NAS 越しでリトライストームになり、
                    # fd を食い潰して Too many open files を招く。
        finally:
            try:
                cursor.close()
            except Exception:
                logger.debug("[DB PRAGMA] cursor close failed db=%s",
                             db_path or "<unknown>", exc_info=True)

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


def _fss_env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def apply_fast_startup_schema_skip() -> bool:
    """旧 fast_startup_runtime_patch._patch_summary_schema_bootstrap() の本文化。

    旧パッチは _bootstrap_summary_schema を no-op 関数へ差し替えていたが、
    ここでは同じ効果をフラグで表現する。呼び出し元は main.py の
    _install_main_runtime_patches のみ (= 旧 install 導線と同じスコープ)。
    FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP=0 で従来どおり補完させられる
    (旧パッチも同じ env で無効化できた)。
    """
    global _SKIP_SUMMARY_SCHEMA_BOOTSTRAP
    if not _fss_env_bool("FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP", True):
        return False
    _SKIP_SUMMARY_SCHEMA_BOOTSTRAP = True
    logger.warning("[SCHEMA BOOTSTRAP] summary schema bootstrap disabled (fast startup)")
    return True


def _bootstrap_summary_schema(engine) -> None:
    """起動時に summary DB の不足列を database/models.py から補完する。"""
    if _SKIP_SUMMARY_SCHEMA_BOOTSTRAP:
        return

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
    # 2026-09-16: positions.db はローカルに書いて NAS/OneDrive へ複製する
    #   (trading/position/positions_db_location.py)。NAS の SMB 劣化で開けなくなり
    #   建玉の記録・保護・決済側の参照が落ちたため。ローカルに無ければ NAS から引き継ぐ。
    try:
        from trading.position import positions_db_location as _pdl
        if _pdl.local_staging_enabled():
            _pdl.bootstrap_local_from_nas()
            position_path = _pdl.positions_db_path()
            _pdl.start_background_sync()
    except Exception:
        logger.exception("[POSITION] local staging setup failed; use NAS path")
    _position_engine = _build_engine(position_path, Base_position, "POSITION")
    _Session_position = sessionmaker(bind=_position_engine)

    ranking_path = get_path("raw_ranking") / f"ranking{today}.db"
    # 2026-09-16: ranking DB は 9/7 からローカル (LOCALAPPDATA/autostock/ranking) が正で
    #   NAS (X:) は複製。ここだけ X: を向いたままで、起動時の build_monitor_symbols
    #   (ranking_snapshot_1min を datetime desc で読む) が X: の遅延で 10 分以上止まり、
    #   main.py が起動できなかった (14:16〜14:30 実測)。ローカルにあればそれを使う。
    try:
        from trading.ranking.ranking_db_location import ranking_db_path as _local_ranking_db_path
        _lp = _local_ranking_db_path()
        if _lp and _lp.exists():
            ranking_path = _lp
    except Exception:
        logger.debug("[RANKING] local ranking db path resolve failed; use raw_ranking", exc_info=True)
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
