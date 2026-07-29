
# ============================================================
# File   : main_database.py
# Version: DATA-COLLECTORS-MAIN-DATABASE-ENTRY-V11-INLINE-PUSH-SUMMARY-AND-SQLITE-PRAGMAS
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH銘柄登録 / PUSH受信 を起動する入口
#   - 既存 main.py とは分離する
#   - 実体は scripts/data_collectors_runner.py に委譲する
#   - main_database.py 経由でも古い PUSH/ranking summary を候補に残さない
#   - main_database.py のコンソールログに時刻を付け、ファイルにも保存する
#   - summary DB の WAL を1分ごとに checkpoint して .db 本体へ反映する
#   - /token 取得直後に実APIで token preflight を行い、認証NGなら子プロセスを起動しない
#   - 前日PUSH summary の global-lag/min_keep fallback を子プロセス既定で無効化する
#
# V11:
#   - core/startup/push_summary_fallback_and_active_price_patch.py (REV5) を
#     trading/ranking/active_symbols/liquidity.py と
#     scheduler_jobs/summary/fallback_loader.py の本文へ完全インライン化し、
#     パッチファイル自体・sitecustomize.py の登録・本ファイルの install() 呼び出しを削除。
#   - core/startup/sqlite_memory_pragmas_patch.py は main.py / data_collectors_runner.py /
#     db_prepare_runner.py / push_receiver_runner.py / yahoo_complement_runner.py /
#     summary_database_runner.py の計7プロセス共通基盤のため元ファイル・
#     sitecustomize.py 側の登録はそのまま残しつつ、main_database.py には
#     ロジック全体を _sqlite_pragmas_* として複製・本文化した（二重管理は承知の上）。
#     sqlite3.connect の二重ラップを避けるため、sitecustomize側で既に
#     wrap済みの場合は自前のwrapをスキップするガードを入れている。
#
# V10:
#   - sitecustomize.py の DB_SYNC_PATCHES (argv判定による暗黙適用) のうち、
#     main_database.py 本文へ未移設だった5パッチを明示的にインライン化。
#     (indicator prevday warmup / yahoo direct upsert conflict /
#      push summary realtime / summary controller latest enrich /
#      active symbol target fill)
#   - db startup scope defaults (schema repair disable / mtf catchup disable)
#     も同様に明示化。sitecustomize.py 側のリストはフォールバックとして維持し、
#     他の子プロセス (data_collectors_runner.py 等) への適用は変更しない。
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from configparser import ConfigParser
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    os.chdir(str(PROJECT_ROOT))
except Exception:
    pass

logger = logging.getLogger(__name__)
_MAIN_DATABASE_LOG_FILE_INSTALLED = False

KABU_API_BASE_URL = "http://localhost:18080/kabusapi"


def _ensure_basic_logging() -> None:
    """Configure timestamped console logging and save main_database logs to file.

    sitecustomize/usercustomize may create root handlers before main_database.py
    starts.  In that case logging.basicConfig() is ignored, so force the formatter
    on existing handlers as well.  The child collector output is captured/saved by
    scripts/data_collectors_runner.py.
    """
    global _MAIN_DATABASE_LOG_FILE_INSTALLED
    try:
        root = logging.getLogger()
        root.setLevel(logging.INFO)

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        if not root.handlers:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            root.addHandler(sh)
        else:
            for h in root.handlers:
                try:
                    h.setFormatter(fmt)
                except Exception:
                    pass

        if not _MAIN_DATABASE_LOG_FILE_INSTALLED:
            try:
                from data_collectors.config import LOG_DIR
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                pid = os.getpid()
                log_path = LOG_DIR / f"main_database_{ts}_{pid}.log"
                fh = logging.FileHandler(log_path, encoding="utf-8")
                fh.setFormatter(fmt)
                root.addHandler(fh)
                _MAIN_DATABASE_LOG_FILE_INSTALLED = True
                logging.getLogger(__name__).warning("[MAIN DATABASE LOG] save to: %s", log_path)
            except Exception:
                logging.getLogger(__name__).exception("[MAIN DATABASE LOG] file handler install failed")
    except Exception:
        pass


# main_database.py プロセス専用の環境変数チューニング値。
# 旧 core/startup/main_database_cpu_guard_env.py から移設。
_CPU_GUARD_FORCE_DEFAULTS = {
    # main_database.py はDB保存専用寄せ。表示/通知/entry系は main.py 側へ寄せる。
    "SUMMARY_DATABASE_RUNNER_DISPLAY": "0",
    "SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY": "0",
    "ENABLE_SUMMARY_ENTRY_TICK": "0",
    "ENABLE_RANKING_SUMMARY_TICK": "0",

    # CPU高止まり対策: PUSH 1m/3m/5mを毎分すべて再計算しない。
    # 1mは毎分、3m/5mは時間境界だけにする。
    # sitecustomize/summary_parallel が 1 を setdefault するため、ここは必ず上書きする。
    "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
    "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
    "SUMMARY_PARALLEL_INTERVAL_WORKERS": "1",
    "SUMMARY_PUSH_BG_INTERVAL_WORKERS": "1",

    # spool flush は毎tick前後では重いので間引く。
    "SUMMARY_SAVE_SPOOL_FLUSH_MIN_INTERVAL_SEC": "120",
    "SUMMARY_SAVE_SPOOL_FLUSH_MAX_FILES": "10",

    # 1回のsummary tickが重すぎる時は次tickを1回休ませる。
    "SUMMARY_DATABASE_SLOW_TICK_SEC": "45",
    "SUMMARY_DATABASE_SKIP_NEXT_ON_SLOW_TICK": "1",

    # MA75 warmupの起動負荷を抑える。
    "PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS": "1",
    "PUSH_INCREMENTAL_MA75_TAIL_ROWS": "90",

    # メモリ余裕を使って SQLite の temp/cache をメモリ寄せする。
    "SQLITE_MEMORY_PRAGMAS_ENABLED": "1",
    "SQLITE_MEMORY_TEMP_STORE": "MEMORY",
    "SQLITE_MEMORY_CACHE_KB": "-65536",
    "SQLITE_BUSY_TIMEOUT_MS": "5000",
    "SQLITE_MMAP_SIZE_BYTES": "268435456",
    "SQLITE_CACHE_SPILL_OFF": "1",
    "RANKING_SQLITE_TEMP_STORE": "MEMORY",
    "SUMMARY_SQLITE_TEMP_STORE": "MEMORY",
    "PUSH_SQLITE_TEMP_STORE": "MEMORY",
    "YAHOO_SQLITE_TEMP_STORE": "MEMORY",
    "RANKING_SQLITE_CACHE_KB": "-65536",
    "SUMMARY_SQLITE_CACHE_KB": "-131072",
    "PUSH_SQLITE_CACHE_KB": "-65536",
    "YAHOO_SQLITE_CACHE_KB": "-65536",

    # summary履歴キャッシュ系。対応モジュールがある場合に有効化される。
    "SUMMARY_HISTORY_MEMORY_CACHE": "1",
    "SUMMARY_HISTORY_CACHE_TTL_SEC": "300",
    "SUMMARY_HISTORY_CACHE_MAX_SYMBOLS": "800",
    "SUMMARY_HISTORY_CACHE_TAIL_ROWS": "90",

    # NAS heartbeat / BLAS thread抑制。
    "AUTOSTOCK_COLLECTOR_PARENT_HEARTBEAT": "0",
    "AUTOSTOCK_DISABLE_NAS_HEARTBEAT": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}


def _install_cpu_guard_env() -> None:
    try:
        for key, value in _CPU_GUARD_FORCE_DEFAULTS.items():
            os.environ[key] = value

        try:
            _install_sqlite_memory_pragmas()
        except Exception:
            pass

        logger.info("[MAIN DATABASE] cpu guard env installed ok=True")
    except Exception:
        logger.exception("[MAIN DATABASE] cpu guard env install failed; continue")


# ============================================================
# sqlite memory pragmas (main_database.py 専用コピー)
# 旧 core/startup/sqlite_memory_pragmas_patch.py から丸ごとコピーして本文化した。
#
# NOTE: この patch は main.py / data_collectors_runner.py / db_prepare_runner.py /
# push_receiver_runner.py / yahoo_complement_runner.py / summary_database_runner.py の
# 計7プロセス共通基盤であり、他の6プロセスは引き続き sitecustomize.py の
# DB_SYNC_PATCHES / SYNC_MAIN_PATCHES 経由で core/startup 側のファイルに依存する
# （そちらは変更していない）。main_database.py だけ本文に複製したため、
# 元ファイルとロジックが乖離しないよう変更時は両方に反映すること。
#
# sitecustomize.py は Python 起動時に main_database.py よりも先に実行され、
# 通常は既に core.startup.sqlite_memory_pragmas_patch 側の sqlite3.connect ラップが
# 適用済みになっている。ここでの二重ラップ（性能劣化・PRAGMA二重適用）を避けるため、
# sqlite3.connect が既に同モジュールでラップ済みかどうかを __module__ で判定し、
# 済みならこちらのラップはスキップする。
# ============================================================
_SQLITE_PRAGMAS_ORIG_CONNECT = sqlite3.connect
_SQLITE_PRAGMAS_INSTALLED = False
_SQLITE_PRAGMAS_SPOOL_FLUSH_PATCHED = False
_SQLITE_PRAGMAS_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_SQLITE_PRAGMAS_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}

_SQLITE_PRAGMAS_SUMMARY_RANKING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rank", "REAL"),
    ("rank_no", "REAL"),
    ("best_rank", "REAL"),
    ("avg_rank", "REAL"),
    ("rank_types_count", "INTEGER"),
    ("ranking_score", "REAL"),
    ("ranking_score_total", "REAL"),
    ("ranking_type", "TEXT"),
    ("rank_types", "TEXT"),
    ("type", "TEXT"),
    ("ranking", "TEXT"),
    ("market", "TEXT"),
    ("current_price", "REAL"),
    ("change_rate", "REAL"),
    ("chg", "REAL"),
    ("trading_volume", "REAL"),
    ("trading_value", "REAL"),
    ("turnover", "REAL"),
    ("turn", "REAL"),
)

_SQLITE_PRAGMAS_SUMMARY_TABLES: tuple[str, ...] = (
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
)


def _sqlite_pragmas_env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _SQLITE_PRAGMAS_TRUE:
            return True
        if raw in _SQLITE_PRAGMAS_FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _sqlite_pragmas_env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, str(default))).strip()))
    except Exception:
        return int(default)


def _sqlite_pragmas_env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except Exception:
        return float(default)


def _sqlite_pragmas_setdefault_env(name: str, value: str) -> None:
    try:
        if not str(os.getenv(name, "")).strip():
            os.environ[name] = value
    except Exception:
        pass


def _sqlite_pragmas_classify_db(database: Any) -> str:
    try:
        p = str(database).replace("\\", "/").lower()
    except Exception:
        return "default"
    if not p or p in {":memory:", "file::memory:"}:
        return "memory"
    name = Path(p).name.lower()
    if "ranking" in name or "/ranking/" in p:
        return "ranking"
    if "summary" in name or "/summary/" in p:
        return "summary"
    if "push" in name or "/push/" in p:
        return "push"
    if "yahoo" in name or "/yahoo/" in p:
        return "yahoo"
    return "default"


def _sqlite_pragmas_looks_like_summary_db_file(database: Any) -> bool:
    try:
        name = Path(str(database or "")).name.lower()
        return name.startswith("summary") and name.endswith(".db")
    except Exception:
        return False


def _sqlite_pragmas_quote_ident_fallback(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sqlite_pragmas_quote_ident(name: str) -> str:
    try:
        from database.sqlite import quote_ident
        return quote_ident(str(name))
    except Exception:
        return _sqlite_pragmas_quote_ident_fallback(str(name))


def _sqlite_pragmas_cache_kb_for(kind: str) -> int:
    names = []
    if kind == "ranking":
        names.append("RANKING_SQLITE_CACHE_KB")
    elif kind == "summary":
        names.append("SUMMARY_SQLITE_CACHE_KB")
    elif kind == "push":
        names.append("PUSH_SQLITE_CACHE_KB")
    elif kind == "yahoo":
        names.append("YAHOO_SQLITE_CACHE_KB")
    names.append("SQLITE_MEMORY_CACHE_KB")

    for name in names:
        raw = os.getenv(name)
        if raw not in (None, ""):
            return _sqlite_pragmas_env_int(name, -65536)
    return -65536


def _sqlite_pragmas_temp_store_for(kind: str) -> str:
    names = []
    if kind == "ranking":
        names.append("RANKING_SQLITE_TEMP_STORE")
    elif kind == "summary":
        names.append("SUMMARY_SQLITE_TEMP_STORE")
    elif kind == "push":
        names.append("PUSH_SQLITE_TEMP_STORE")
    elif kind == "yahoo":
        names.append("YAHOO_SQLITE_TEMP_STORE")
    names.append("SQLITE_MEMORY_TEMP_STORE")

    for name in names:
        raw = os.getenv(name)
        if raw not in (None, ""):
            return str(raw).strip().upper()
    return "MEMORY"


def _sqlite_pragmas_table_exists(cur: Any, table: str) -> bool:
    try:
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _sqlite_pragmas_ensure_columns_with_cursor(cur: Any, table: str, columns: tuple[tuple[str, str], ...], label: str) -> list[str]:
    q = _sqlite_pragmas_quote_ident(table)
    try:
        cur.execute(f"PRAGMA table_info({q})")
        existing = {str(row[1]) for row in cur.fetchall()}
    except Exception:
        logger.debug("[%s] table_info failed table=%s", label, table, exc_info=True)
        return []

    added: list[str] = []
    for col, decl in columns:
        if col in existing:
            continue
        try:
            cur.execute(f"ALTER TABLE {q} ADD COLUMN {_sqlite_pragmas_quote_ident(col)} {decl}")
            added.append(col)
            existing.add(col)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                existing.add(col)
                continue
            raise
    if added:
        logger.warning("[%s] table=%s added_columns=%s", label, table, added)
    return added


def _sqlite_pragmas_ensure_summary_ranking_columns_on_connection(conn: sqlite3.Connection, database: Any) -> None:
    if _sqlite_pragmas_env_bool("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", False):
        return
    if _sqlite_pragmas_classify_db(database) != "summary":
        return
    try:
        cur = conn.cursor()
        for table in _SQLITE_PRAGMAS_SUMMARY_TABLES:
            if not _sqlite_pragmas_table_exists(cur, table):
                continue
            _sqlite_pragmas_ensure_columns_with_cursor(cur, table, _SQLITE_PRAGMAS_SUMMARY_RANKING_COLUMNS, "SUMMARY RANKING SCHEMA REPAIR")
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        logger.debug("[SUMMARY RANKING SCHEMA REPAIR] connection repair failed database=%s", database, exc_info=True)


def _sqlite_pragmas_force_summary_lock_env() -> None:
    _sqlite_pragmas_setdefault_env("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", "60000")
    _sqlite_pragmas_setdefault_env("SUMMARY_SQLITE_TIMEOUT", "60")
    _sqlite_pragmas_setdefault_env("SUMMARY_SQLITE_CACHE_KB", "-131072")
    _sqlite_pragmas_setdefault_env("SUMMARY_SQLITE_TEMP_STORE", "MEMORY")

    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_SQLITE_TIMEOUT", "60")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_BUSY_TIMEOUT_MS", "60000")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_UPDATE_CHUNK_SIZE", "50")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_LOCK_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_LOCK_SLEEP_BASE", "0.50")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_SKIP_IF_BUSY", "1")

    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_SQLITE_TIMEOUT", "60")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_BUSY_TIMEOUT_MS", "60000")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_LOCK_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_UPSERT_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_BUSY_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_LOCK_SLEEP_BASE", "0.50")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_CHUNK_SIZE", "100")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_UPSERT_CHUNK_SIZE", "100")


def _sqlite_pragmas_configure_summary_connection_extra(conn: sqlite3.Connection) -> None:
    busy_ms = max(60000, _sqlite_pragmas_env_int("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", 60000))
    pragmas = [
        f"PRAGMA busy_timeout={busy_ms}",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA wal_autocheckpoint=1000",
    ]
    for sql in pragmas:
        try:
            conn.execute(sql)
        except Exception:
            pass


def _sqlite_pragmas_apply_pragmas(conn: sqlite3.Connection, database: Any) -> None:
    if not _sqlite_pragmas_env_bool("SQLITE_MEMORY_PRAGMAS_ENABLED", True):
        return

    kind = _sqlite_pragmas_classify_db(database)
    if kind == "memory":
        return

    try:
        timeout_ms = max(0, _sqlite_pragmas_env_int("SQLITE_BUSY_TIMEOUT_MS", 5000))
        if timeout_ms:
            conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    except Exception:
        pass

    try:
        temp_store = _sqlite_pragmas_temp_store_for(kind)
        if temp_store in {"MEMORY", "2"}:
            conn.execute("PRAGMA temp_store=MEMORY")
        elif temp_store in {"FILE", "1"}:
            conn.execute("PRAGMA temp_store=FILE")
    except Exception:
        pass

    try:
        cache_kb = _sqlite_pragmas_cache_kb_for(kind)
        if cache_kb:
            conn.execute(f"PRAGMA cache_size={int(cache_kb)}")
    except Exception:
        pass

    try:
        mmap_bytes = max(0, _sqlite_pragmas_env_int("SQLITE_MMAP_SIZE_BYTES", 268435456))
        if mmap_bytes:
            conn.execute(f"PRAGMA mmap_size={mmap_bytes}")
    except Exception:
        pass

    try:
        if _sqlite_pragmas_env_bool("SQLITE_CACHE_SPILL_OFF", True):
            conn.execute("PRAGMA cache_spill=OFF")
    except Exception:
        pass

    if _sqlite_pragmas_looks_like_summary_db_file(database):
        try:
            _sqlite_pragmas_configure_summary_connection_extra(conn)
        except Exception:
            pass

    try:
        _sqlite_pragmas_ensure_summary_ranking_columns_on_connection(conn, database)
    except Exception:
        pass


def _sqlite_pragmas_patched_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    if _sqlite_pragmas_looks_like_summary_db_file(database):
        try:
            current_timeout = float(kwargs.get("timeout", 0) or 0)
        except Exception:
            current_timeout = 0.0
        kwargs["timeout"] = max(current_timeout, max(60.0, _sqlite_pragmas_env_float("SUMMARY_SQLITE_TIMEOUT", 60.0)))

    conn = _SQLITE_PRAGMAS_ORIG_CONNECT(database, *args, **kwargs)
    try:
        _sqlite_pragmas_apply_pragmas(conn, database)
    except Exception:
        try:
            logger.debug("[SQLITE MEMORY PRAGMAS] apply failed database=%s", database, exc_info=True)
        except Exception:
            pass
    return conn


def _sqlite_pragmas_install_summary_spool_flush_patch() -> bool:
    global _SQLITE_PRAGMAS_SPOOL_FLUSH_PATCHED
    if _SQLITE_PRAGMAS_SPOOL_FLUSH_PATCHED:
        return True
    if _sqlite_pragmas_env_bool("DISABLE_SUMMARY_SPOOL_FLUSH_BULK_PATCH", False):
        return False

    try:
        import trading.summary.persistence.summary_save_spool as spool
        import trading.summary.persistence.summary_saver_bulk as saver

        old = getattr(spool, "_save_direct", None)
        if not callable(old):
            return False
        if getattr(old, "_summary_spool_flush_bulk_patch", False):
            _SQLITE_PRAGMAS_SPOOL_FLUSH_PATCHED = True
            return True

        def _patched_save_direct(df: Any, *, interval: int, source: str, date_yyyymmdd: str) -> int:
            try:
                saved = saver.bulk_upsert_summary(
                    df,
                    interval=int(interval),
                    lock_timeout_sec=float(os.getenv("SUMMARY_SPOOL_FLUSH_LOCK_TIMEOUT_SEC", "8.0")),
                    skip_if_busy=True,
                    latest_only=True,
                    save_reason="spool_recovery_flush",
                )
                if int(saved or 0) > 0:
                    return int(saved)
            except Exception:
                logger.exception(
                    "[SUMMARY SAVE SPOOL PATCH] bulk flush failed interval=%s source=%s date=%s -> fallback direct",
                    interval,
                    source,
                    date_yyyymmdd,
                )
            return int(old(df, interval=interval, source=source, date_yyyymmdd=date_yyyymmdd) or 0)

        _patched_save_direct._summary_spool_flush_bulk_patch = True  # type: ignore[attr-defined]
        _patched_save_direct._original = old  # type: ignore[attr-defined]
        spool._save_direct = _patched_save_direct
        _SQLITE_PRAGMAS_SPOOL_FLUSH_PATCHED = True
        logger.warning("[SUMMARY SAVE SPOOL PATCH] installed bulk_flush=True (main_database.py own copy)")
        return True
    except Exception:
        logger.exception("[SUMMARY SAVE SPOOL PATCH] install failed (main_database.py own copy)")
        return False


def _install_sqlite_memory_pragmas() -> bool:
    global _SQLITE_PRAGMAS_INSTALLED

    _sqlite_pragmas_force_summary_lock_env()
    spool_ok = _sqlite_pragmas_install_summary_spool_flush_patch()

    if _SQLITE_PRAGMAS_INSTALLED:
        return True
    if not _sqlite_pragmas_env_bool("SQLITE_MEMORY_PRAGMAS_ENABLED", True):
        return bool(spool_ok)

    if getattr(sqlite3.connect, "__module__", "") == "core.startup.sqlite_memory_pragmas_patch":
        _SQLITE_PRAGMAS_INSTALLED = True
        logger.warning(
            "[SQLITE MEMORY PRAGMAS] skip own wrap: sqlite3.connect already wrapped by "
            "sitecustomize's core.startup.sqlite_memory_pragmas_patch"
        )
        return True

    try:
        sqlite3.connect = _sqlite_pragmas_patched_connect  # type: ignore[assignment]
        _SQLITE_PRAGMAS_INSTALLED = True
        logger.warning(
            "[SQLITE MEMORY PRAGMAS] installed (main_database.py own copy) temp_store=%s cache_kb=%s mmap=%s spill_off=%s summary_busy_ms=%s spool_bulk=%s",
            os.getenv("SQLITE_MEMORY_TEMP_STORE", "MEMORY"),
            os.getenv("SQLITE_MEMORY_CACHE_KB", "-65536"),
            os.getenv("SQLITE_MMAP_SIZE_BYTES", "268435456"),
            os.getenv("SQLITE_CACHE_SPILL_OFF", "1"),
            os.getenv("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", "60000"),
            spool_ok,
        )
        return True
    except Exception:
        logger.exception("[SQLITE MEMORY PRAGMAS] install failed (main_database.py own copy)")
        return bool(spool_ok)


# summary WAL checkpoint loop. 旧 core/startup/summary_wal_checkpoint_patch.py から移設。
_WAL_CHECKPOINT_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_WAL_CHECKPOINT_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}
_WAL_CHECKPOINT_MODES = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
_WAL_CHECKPOINT_INSTALLED = False
_WAL_CHECKPOINT_THREAD: threading.Thread | None = None


def _wal_checkpoint_env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if raw in _WAL_CHECKPOINT_TRUE:
        return True
    if raw in _WAL_CHECKPOINT_FALSE:
        return False
    return bool(default)


def _wal_checkpoint_env_float(name: str, default: float) -> float:
    try:
        raw = str(os.getenv(name, "")).strip()
        return float(raw) if raw else float(default)
    except Exception:
        return float(default)


def _wal_checkpoint_env_int(name: str, default: int) -> int:
    try:
        raw = str(os.getenv(name, "")).strip()
        return int(float(raw)) if raw else int(default)
    except Exception:
        return int(default)


def _wal_checkpoint_mode() -> str:
    mode = str(os.getenv("SUMMARY_WAL_CHECKPOINT_MODE", "PASSIVE")).strip().upper()
    return mode if mode in _WAL_CHECKPOINT_MODES else "PASSIVE"


def _wal_checkpoint_summary_db_path() -> Path:
    from data_collectors.config import summary_db_path
    return Path(summary_db_path())


def _wal_checkpoint_once() -> bool:
    db_path = _wal_checkpoint_summary_db_path()
    if not db_path.exists():
        logger.debug("[SUMMARY WAL CHECKPOINT] skip db missing path=%s", db_path)
        return False

    mode = _wal_checkpoint_mode()
    busy_timeout_ms = max(1000, _wal_checkpoint_env_int("SUMMARY_WAL_CHECKPOINT_BUSY_TIMEOUT_MS", 5000))
    wal_path = Path(str(db_path) + "-wal")
    try:
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
    except Exception:
        wal_size = 0

    try:
        conn = sqlite3.connect(str(db_path), timeout=max(1.0, busy_timeout_ms / 1000.0))
        try:
            conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode=WAL")
            result = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            conn.commit()
        finally:
            conn.close()
        logger.info(
            "[SUMMARY WAL CHECKPOINT] done mode=%s db=%s wal_size=%s result=%s",
            mode, db_path, wal_size, result,
        )
        return True
    except sqlite3.OperationalError as exc:
        logger.warning("[SUMMARY WAL CHECKPOINT] skip busy mode=%s db=%s err=%s", mode, db_path, exc)
        return False
    except Exception:
        logger.exception("[SUMMARY WAL CHECKPOINT] failed db=%s", db_path)
        return False


def _wal_checkpoint_loop() -> None:
    initial_delay = max(0.0, _wal_checkpoint_env_float("SUMMARY_WAL_CHECKPOINT_INITIAL_DELAY_SEC", 20.0))
    interval = max(10.0, _wal_checkpoint_env_float("SUMMARY_WAL_CHECKPOINT_INTERVAL_SEC", 60.0))
    if initial_delay:
        time.sleep(initial_delay)
    while True:
        if _wal_checkpoint_env_bool("SUMMARY_WAL_CHECKPOINT_ENABLED", True):
            _wal_checkpoint_once()
        time.sleep(interval)


def _install_summary_wal_checkpoint() -> None:
    """Install a 1-minute WAL checkpoint loop for summaryYYYYMMDD.db.

    This copies committed frames from summaryYYYYMMDD.db-wal into the .db file
    without changing the writer logic.  PASSIVE mode is the default to avoid
    increasing writer/reader lock contention on NAS SQLite.
    """
    global _WAL_CHECKPOINT_INSTALLED, _WAL_CHECKPOINT_THREAD
    try:
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_ENABLED", "1")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_INTERVAL_SEC", "60")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_MODE", "PASSIVE")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_BUSY_TIMEOUT_MS", "5000")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_INITIAL_DELAY_SEC", "20")

        if _WAL_CHECKPOINT_INSTALLED:
            ok = True
        elif not _wal_checkpoint_env_bool("SUMMARY_WAL_CHECKPOINT_ENABLED", True):
            logger.warning("[SUMMARY WAL CHECKPOINT] disabled by env")
            ok = False
        else:
            _WAL_CHECKPOINT_THREAD = threading.Thread(
                target=_wal_checkpoint_loop, name="summary-wal-checkpoint-loop", daemon=True
            )
            _WAL_CHECKPOINT_THREAD.start()
            _WAL_CHECKPOINT_INSTALLED = True
            ok = True

        logger.warning("[MAIN DATABASE] summary wal checkpoint installed ok=%s", ok)
    except Exception:
        logger.exception("[MAIN DATABASE] summary wal checkpoint install failed; continue")


def _install_summary_stale_guard() -> None:
    """Install stale summary guard for main_database.py / child collectors.

    main.py has its own runtime patch bootstrap, but main_database.py is a
    separate entrypoint.  When data collectors publish merged summary through
    core.global_context.context, this guard prevents old PUSH/ranking rows from
    staying alive as fresh candidates.
    """
    try:
        # Defaults are inherited by any child collector processes.
        # Important: default must be hard-drop.  Keeping latest-per-symbol on
        # global lag caused 2026-06-30 15:19 rows to remain alive on 2026-07-01
        # during market session.
        defaults = {
            "SUMMARY_STALE_GUARD_ENABLED": "1",
            "SUMMARY_STALE_STRICT_TODAY_ONLY": "1",
            "SUMMARY_STALE_KEEP_LATEST_PER_SYMBOL_ON_GLOBAL_LAG": "0",
            "SUMMARY_STALE_GLOBAL_LAG_GRACE_SEC": "30",
            "PUSH_SUMMARY_1MIN_MAX_AGE_SEC": "120",
            "PUSH_SUMMARY_3MIN_MAX_AGE_SEC": "240",
            "PUSH_SUMMARY_5MIN_MAX_AGE_SEC": "420",
            "PUSH_SUMMARY_1MIN_KEEP_ROWS": "0",
            "PUSH_SUMMARY_3MIN_KEEP_ROWS": "0",
            "PUSH_SUMMARY_5MIN_KEEP_ROWS": "0",
            "RANKING_SUMMARY_1MIN_MAX_AGE_SEC": "180",
            "RANKING_SUMMARY_3MIN_MAX_AGE_SEC": "300",
            "RANKING_SUMMARY_5MIN_MAX_AGE_SEC": "480",
            "RANKING_SUMMARY_1MIN_KEEP_ROWS": "0",
            "RANKING_SUMMARY_3MIN_KEEP_ROWS": "0",
            "RANKING_SUMMARY_5MIN_KEEP_ROWS": "0",
            # User rule: recent 5-bar turnover target is 1,000,000 yen.
            # The old 5,000,000 default can delete all candidates around open.
            "LIQUIDITY_MIN_TURNOVER": "1000000",
            "SUMMARY_ENTRY_ALLOW_UNREADY_5MIN_AT_OPEN": "1",
        }
        for key, value in defaults.items():
            os.environ.setdefault(key, value)

        # drop_stale_summary_rows 本体は core/global_context/context.py へ移設済みのため、
        # ここでは呼び出し先プロセス全体で読まれる環境変数デフォルトの設定だけを行う。
        logger.warning(
            "[MAIN DATABASE] summary stale guard env defaults set strict_today=%s global_lag_keep=%s liquidity_min_turnover=%s",
            os.getenv("SUMMARY_STALE_STRICT_TODAY_ONLY"),
            os.getenv("SUMMARY_STALE_KEEP_LATEST_PER_SYMBOL_ON_GLOBAL_LAG"),
            os.getenv("LIQUIDITY_MIN_TURNOVER"),
        )
    except Exception:
        logger.exception("[MAIN DATABASE] summary stale guard env defaults setup failed; continue")


def _install_db_startup_scope_defaults() -> None:
    """Set the main_database.py-side resolution of sitecustomize's DB startup scope.

    Moved-in equivalent of sitecustomize.py's _configure_db_startup_scope_defaults()
    for the non-db_prepare branch (main_database.py is never db_prepare_runner.py,
    so that branch can never apply here). sitecustomize.py already sets these via
    setdefault before main_database.py's own code runs; this call makes the same
    resolution explicit and independent of sys.argv sniffing.
    """
    try:
        os.environ.setdefault("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", "1")
        os.environ.setdefault("SUMMARY_RANKING_SCHEMA_REPAIR_SCOPE", "db_prepare_only")
        os.environ.setdefault("DISABLE_SUMMARY_MTF_CATCHUP", "1")
        logger.warning(
            "[MAIN DATABASE] db startup scope defaults installed schema_repair_disabled=%s mtf_catchup_disabled=%s",
            os.getenv("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH"),
            os.getenv("DISABLE_SUMMARY_MTF_CATCHUP"),
        )
    except Exception:
        logger.exception("[MAIN DATABASE] db startup scope defaults install failed; continue")


def _install_push_summary_realtime() -> None:
    """Force PUSH DB writer env, start the writer singleton, and schedule a bootstrap rebuild.

    The function-wrap parts of the old core/startup/push_summary_realtime_patch.py
    (StreamDBWriter.flush trigger, start_push_stream forced writer injection, 401
    refresh suppression) are now baked directly into their real target modules
    (trading/push/push_db_writer.py, trading/push/push_stream/__init__.py,
    force_cancel_loop.py, trading/position/kabu_position_reader.py,
    kabu_api/positions.py), gated at call time by
    data_collectors.split_mode.is_data_collector_process(). Only the startup-time
    side effects (not tied to any single function) remain here.
    """
    try:
        from trading.push.push_stream import (
            _force_push_writer_env_if_database_process,
            _ensure_stream_writer_singleton_started,
        )
        _force_push_writer_env_if_database_process()
        writer = _ensure_stream_writer_singleton_started()
        logger.warning("[MAIN DATABASE] push db writer singleton ensured ok=%s", writer is not None)
    except Exception:
        logger.exception("[MAIN DATABASE] push db writer singleton ensure failed; continue")

    try:
        from trading.push.push_summary_rebuild_trigger import schedule_bootstrap_rebuild
        schedule_bootstrap_rebuild()
    except Exception:
        logger.exception("[MAIN DATABASE] push summary bootstrap rebuild schedule failed; continue")


def _read_api_password_from_settings() -> str:
    conf = ConfigParser()
    conf.read(str(PROJECT_ROOT / "settings.ini"), encoding="utf-8")

    if conf.has_section("aukabu"):
        return conf.get("aukabu", "apipassword", fallback="")

    if conf.has_section("kabusapi"):
        return conf.get("kabusapi", "apipassword", fallback="")

    return ""


def _safe_content_text(value: Any, limit: int = 240) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _is_api_key_mismatch(content: Any) -> bool:
    try:
        if isinstance(content, dict):
            code = str(content.get("Code") or "")
            msg = str(content.get("Message") or "")
            return code == "4001009" or "APIキー不一致" in msg
        s = str(content)
        return "4001009" in s or "APIキー不一致" in s
    except Exception:
        return False


def _kabu_preflight_request(token: str, endpoint: str, timeout: float = 5.0) -> tuple[bool, int | None, Any]:
    url = f"{KABU_API_BASE_URL}{endpoint}"
    req = urllib.request.Request(
        url,
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": str(token or ""),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="ignore")
            try:
                content = json.loads(raw) if raw else {}
            except Exception:
                content = raw
            return True, int(getattr(res, "status", 200) or 200), content
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="ignore")
        except Exception:
            raw = ""
        try:
            content = json.loads(raw) if raw else str(e)
        except Exception:
            content = raw or str(e)
        status = int(getattr(e, "code", 0) or 0)
        return False, status, content
    except Exception as e:
        return False, None, str(e)


def _preflight_kabu_token(token: str) -> bool:
    """Validate the freshly acquired token with real kabu API calls before spawning children.

    /token can succeed while subsequent APIs still reject X-API-KEY.  This guard
    catches that condition early and prevents push_receiver/ranking_collector from
    starting with an unusable token.
    """
    token = str(token or "").strip()
    if not token:
        logger.error("[MAIN DATABASE] kabu token preflight failed: empty token")
        return False

    endpoints = [
        "/positions",
        "/wallet/cash",
    ]

    auth_failures: list[tuple[str, int | None, Any]] = []
    transport_failures: list[tuple[str, int | None, Any]] = []

    for endpoint in endpoints:
        ok, status, content = _kabu_preflight_request(token, endpoint)
        if ok:
            logger.warning(
                "[MAIN DATABASE] kabu token preflight ok endpoint=%s status=%s token_len=%d",
                endpoint,
                status,
                len(token),
            )
            os.environ["KABU_TOKEN_PREFLIGHT_OK"] = "1"
            os.environ["KABU_TOKEN_PREFLIGHT_ENDPOINT"] = endpoint
            return True

        if status in (401, 403) or _is_api_key_mismatch(content):
            auth_failures.append((endpoint, status, content))
            logger.error(
                "[MAIN DATABASE] kabu token preflight auth failed endpoint=%s status=%s content=%s token_len=%d",
                endpoint,
                status,
                _safe_content_text(content),
                len(token),
            )
            continue

        # 400/404 etc. means the request reached kabu Station and was not rejected
        # by API key.  For authentication preflight, this is enough to prove token
        # was accepted; log it as accepted-with-endpoint-error.
        if status is not None:
            logger.warning(
                "[MAIN DATABASE] kabu token preflight accepted endpoint=%s status=%s content=%s token_len=%d",
                endpoint,
                status,
                _safe_content_text(content),
                len(token),
            )
            os.environ["KABU_TOKEN_PREFLIGHT_OK"] = "1"
            os.environ["KABU_TOKEN_PREFLIGHT_ENDPOINT"] = endpoint
            return True

        transport_failures.append((endpoint, status, content))
        logger.error(
            "[MAIN DATABASE] kabu token preflight transport failed endpoint=%s error=%s",
            endpoint,
            _safe_content_text(content),
        )

    if auth_failures:
        endpoint, status, content = auth_failures[-1]
        logger.error(
            "[MAIN DATABASE] abort: token was obtained but rejected by kabu API endpoint=%s status=%s content=%s. "
            "Please restart kabu Station, enable API, and confirm settings.ini apipassword matches kabu Station.",
            endpoint,
            status,
            _safe_content_text(content),
        )
    elif transport_failures:
        endpoint, _status, content = transport_failures[-1]
        logger.error(
            "[MAIN DATABASE] abort: kabu API preflight could not connect endpoint=%s error=%s. "
            "Please confirm kabu Station API is running on localhost:18080.",
            endpoint,
            _safe_content_text(content),
        )
    else:
        logger.error("[MAIN DATABASE] abort: kabu token preflight failed for unknown reason")
    return False


def _bootstrap_kabu_token_for_data_collectors() -> bool:
    _ensure_basic_logging()

    try:
        api_password = _read_api_password_from_settings()

        if not api_password:
            logger.error(
                "[MAIN DATABASE] token bootstrap failed: settings.ini apipassword missing"
            )
            return False
        from token_manager import refresh_token, get_valid_token

        token = refresh_token(api_password)

        if not token:
            logger.error("[MAIN DATABASE] token bootstrap failed: empty token returned")
            return False

        try:
            _ = get_valid_token()
        except Exception:
            pass

        if not _preflight_kabu_token(str(token)):
            logger.error(
                "[MAIN DATABASE] token bootstrap failed: preflight rejected token; children will not start"
            )
            return False

        try:
            from global_state import global_data
            global_data.token_value = token
        except Exception:
            logger.debug("[MAIN DATABASE] global_data.token_value set skipped", exc_info=True)

        logger.info(
            "[MAIN DATABASE] kabu token refreshed and preflight passed for data collectors token_len=%s",
            len(str(token)),
        )
        return True

    except Exception:
        logger.exception("[MAIN DATABASE] token bootstrap failed")
        return False


def main() -> int:
    _ensure_basic_logging()

    try:
        os.chdir(str(PROJECT_ROOT))
    except Exception:
        logger.exception("[MAIN DATABASE] chdir PROJECT_ROOT failed path=%s", PROJECT_ROOT)
        return 1

    logger.info("========== MAIN DATABASE BOOT START ==========")
    logger.info("[MAIN DATABASE] PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("[MAIN DATABASE] cwd=%s", os.getcwd())

    _install_db_startup_scope_defaults()
    _install_cpu_guard_env()
    _install_summary_wal_checkpoint()
    _install_summary_stale_guard()
    _install_push_summary_realtime()

    try:
        from data_collectors.split_mode import mark_as_data_collector_process
        mark_as_data_collector_process()
    except Exception:
        logger.exception("[MAIN DATABASE] failed to mark data collector process")
        return 1

    if not _bootstrap_kabu_token_for_data_collectors():
        logger.error(
            "[MAIN DATABASE] abort because token bootstrap/preflight failed. "
            "Please confirm kabu Station is running, API is enabled, and API password is correct."
        )
        return 1

    try:
        from scripts.data_collectors_runner import main as data_collectors_main
    except Exception:
        logger.exception("[MAIN DATABASE] failed to import scripts.data_collectors_runner.main")
        return 1

    return int(data_collectors_main())


if __name__ == "__main__":
    raise SystemExit(main())
