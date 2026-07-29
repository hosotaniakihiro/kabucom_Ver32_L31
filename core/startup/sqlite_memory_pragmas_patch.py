# ============================================================
# File   : core/startup/sqlite_memory_pragmas_patch.py
# Version: SQLITE-MEMORY-PRAGMAS-PATCH-V5-UNIFIED-CONNECT
# ------------------------------------------------------------
# Purpose:
#   main_database.py / data collector 子プロセスの SQLite 接続に対して、
#   メモリに余裕がある環境向けの PRAGMA を自動適用する。
#
#   V2:
#   - ranking の種類別 legacy table が既存DBに残っている場合も、
#     writer保存前に不足列を ALTER TABLE で自動補修する。
#   - 例: table 値上がり率_ALL has no column named rank を防止する。
#
#   V3:
#   - summary DB の stock_summary_1/3/5min にランキング由来列を自動追加する。
#     summary_saver_bulk が rank/ranking_score/ranking_type を drop する問題を防止。
#   - SUMMARY SAVE SPOOL の flush を summary_saver_bulk 経由にして、列不一致や型差分で
#     failed_files が残り続ける問題を緩和する。
#
#   V4:
#   - ranking legacy table 補修は trading/ranking/ranking_db_writer.py の
#     _ensure_legacy_table 本体へ移設。
#   - summary ranking schema 補修は trading/summary/persistence/summary_saver_bulk.py の
#     _get_table_columns_from_engine 本体へ移設。
#   - sqlite3.connect 経由の PRAGMA / summary schema 補修 (下記 _apply_pragmas) は
#     stdlib 全体への上書きのため、このファイルに残す。
#
#   V5:
#   - 旧 core/startup/summary_sqlite_lock_tolerance_patch.py を統合。
#     summary*.db への sqlite3.connect が二重に monkeypatch されていたのを
#     この1本の _patched_connect にまとめた。
#   - バグ修正: 旧 summary_sqlite_lock_tolerance_patch は summary専用のつもりの
#     busy_timeout(60秒)を共通環境変数 SQLITE_BUSY_TIMEOUT_MS に書き込んでいたため、
#     summary以外の全DB接続にも60秒busy_timeoutが漏れていた。
#     統合後は summary専用の busy_timeout は SUMMARY_SQLITE_BUSY_TIMEOUT_MS だけに
#     設定し、共通の SQLITE_BUSY_TIMEOUT_MS (既定5秒) には触れない。
#
# Notes:
#   - 既存コードの sqlite3.connect 呼び出しを横取りし、接続直後に軽量PRAGMAを適用する。
#   - 失敗しても接続自体は止めない。
#   - 書き込み永続性を落とす PRAGMA はここでは変更しない。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ORIG_CONNECT = sqlite3.connect
_INSTALLED = False
_SUMMARY_SPOOL_FLUSH_PATCHED = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}


SUMMARY_RANKING_COLUMNS: tuple[tuple[str, str], ...] = (
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


SUMMARY_TABLES: tuple[str, ...] = (
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, str(default))).strip()))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except Exception:
        return float(default)


def _setdefault_env(name: str, value: str) -> None:
    try:
        if not str(os.getenv(name, "")).strip():
            os.environ[name] = value
    except Exception:
        pass


def _classify_db(database: Any) -> str:
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


def _looks_like_summary_db_file(database: Any) -> bool:
    """summaryYYYYMMDD.db のような実ファイル名だけを狭く判定する。

    _classify_db() は "summary" を含むパス全般を緩く拾うが、接続timeoutの
    底上げ・WAL/synchronous等の追加PRAGMAは実際のsummary DB本体だけに限定する
    (旧 core/startup/summary_sqlite_lock_tolerance_patch.py の判定基準を踏襲)。
    """
    try:
        name = Path(str(database or "")).name.lower()
        return name.startswith("summary") and name.endswith(".db")
    except Exception:
        return False


def _quote_ident_fallback(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _quote_ident(name: str) -> str:
    try:
        from database.sqlite import quote_ident
        return quote_ident(str(name))
    except Exception:
        return _quote_ident_fallback(str(name))


def _cache_kb_for(kind: str) -> int:
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
            return _env_int(name, -65536)
    return -65536


def _temp_store_for(kind: str) -> str:
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


def _table_exists(cur: Any, table: str) -> bool:
    try:
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _ensure_columns_with_cursor(cur: Any, table: str, columns: tuple[tuple[str, str], ...], label: str) -> list[str]:
    q = _quote_ident(table)
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
            cur.execute(f"ALTER TABLE {q} ADD COLUMN {_quote_ident(col)} {decl}")
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


def _ensure_summary_ranking_columns_on_connection(conn: sqlite3.Connection, database: Any) -> None:
    if _env_bool("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", False):
        return
    if _classify_db(database) != "summary":
        return
    try:
        cur = conn.cursor()
        for table in SUMMARY_TABLES:
            if not _table_exists(cur, table):
                continue
            _ensure_columns_with_cursor(cur, table, SUMMARY_RANKING_COLUMNS, "SUMMARY RANKING SCHEMA REPAIR")
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        logger.debug("[SUMMARY RANKING SCHEMA REPAIR] connection repair failed database=%s", database, exc_info=True)


def _force_summary_lock_env() -> None:
    """summary DB向けのロック耐性ENVデフォルトを設定する。

    旧 core/startup/summary_sqlite_lock_tolerance_patch.py から移設。
    NOTE: SQLITE_BUSY_TIMEOUT_MS はDB種別を問わない共通デフォルト値のため、
    ここでは書き換えない。summary専用の値は SUMMARY_SQLITE_BUSY_TIMEOUT_MS だけに
    設定する (旧バージョンでの全DBへの漏れを修正)。
    """
    _setdefault_env("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", "60000")
    _setdefault_env("SUMMARY_SQLITE_TIMEOUT", "60")
    _setdefault_env("SUMMARY_SQLITE_CACHE_KB", "-131072")
    _setdefault_env("SUMMARY_SQLITE_TEMP_STORE", "MEMORY")

    # MTF indicator fill。大きいchunkがロックを長く持つため小分け、retryは長め。
    _setdefault_env("SUMMARY_MTF_INDICATOR_SQLITE_TIMEOUT", "60")
    _setdefault_env("SUMMARY_MTF_INDICATOR_BUSY_TIMEOUT_MS", "60000")
    _setdefault_env("SUMMARY_MTF_INDICATOR_UPDATE_CHUNK_SIZE", "50")
    _setdefault_env("SUMMARY_MTF_INDICATOR_LOCK_RETRIES", "20")
    _setdefault_env("SUMMARY_MTF_INDICATOR_LOCK_SLEEP_BASE", "0.50")
    _setdefault_env("SUMMARY_MTF_INDICATOR_SKIP_IF_BUSY", "1")

    # MTF catchup 側で使われる可能性のある名称をまとめて安全側に寄せる。
    # 未使用ENVは無害。
    _setdefault_env("SUMMARY_MTF_CATCHUP_SQLITE_TIMEOUT", "60")
    _setdefault_env("SUMMARY_MTF_CATCHUP_BUSY_TIMEOUT_MS", "60000")
    _setdefault_env("SUMMARY_MTF_CATCHUP_LOCK_RETRIES", "20")
    _setdefault_env("SUMMARY_MTF_CATCHUP_RETRIES", "20")
    _setdefault_env("SUMMARY_MTF_CATCHUP_UPSERT_RETRIES", "20")
    _setdefault_env("SUMMARY_MTF_CATCHUP_BUSY_RETRIES", "20")
    _setdefault_env("SUMMARY_MTF_CATCHUP_LOCK_SLEEP_BASE", "0.50")
    _setdefault_env("SUMMARY_MTF_CATCHUP_CHUNK_SIZE", "100")
    _setdefault_env("SUMMARY_MTF_CATCHUP_UPSERT_CHUNK_SIZE", "100")


def _configure_summary_connection_extra(conn: sqlite3.Connection) -> None:
    """summary DB本体だけに追加で効かせるPRAGMA (busy_timeout底上げ・WAL・synchronous)。

    旧 core/startup/summary_sqlite_lock_tolerance_patch.py から移設。
    """
    busy_ms = max(60000, _env_int("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", 60000))
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
            # journal_modeなどはタイミングにより失敗しても致命傷にしない。
            pass


def _apply_pragmas(conn: sqlite3.Connection, database: Any) -> None:
    if not _env_bool("SQLITE_MEMORY_PRAGMAS_ENABLED", True):
        return

    kind = _classify_db(database)
    if kind == "memory":
        return

    try:
        timeout_ms = max(0, _env_int("SQLITE_BUSY_TIMEOUT_MS", 5000))
        if timeout_ms:
            conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    except Exception:
        pass

    try:
        temp_store = _temp_store_for(kind)
        if temp_store in {"MEMORY", "2"}:
            conn.execute("PRAGMA temp_store=MEMORY")
        elif temp_store in {"FILE", "1"}:
            conn.execute("PRAGMA temp_store=FILE")
    except Exception:
        pass

    try:
        cache_kb = _cache_kb_for(kind)
        if cache_kb:
            conn.execute(f"PRAGMA cache_size={int(cache_kb)}")
    except Exception:
        pass

    try:
        mmap_bytes = max(0, _env_int("SQLITE_MMAP_SIZE_BYTES", 268435456))
        if mmap_bytes:
            conn.execute(f"PRAGMA mmap_size={mmap_bytes}")
    except Exception:
        # NAS/Windows環境では mmap が無効なことがある。失敗しても無視する。
        pass

    try:
        if _env_bool("SQLITE_CACHE_SPILL_OFF", True):
            conn.execute("PRAGMA cache_spill=OFF")
    except Exception:
        pass

    if _looks_like_summary_db_file(database):
        try:
            _configure_summary_connection_extra(conn)
        except Exception:
            pass

    try:
        _ensure_summary_ranking_columns_on_connection(conn, database)
    except Exception:
        pass


def _patched_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    if _looks_like_summary_db_file(database):
        try:
            current_timeout = float(kwargs.get("timeout", 0) or 0)
        except Exception:
            current_timeout = 0.0
        kwargs["timeout"] = max(current_timeout, max(60.0, _env_float("SUMMARY_SQLITE_TIMEOUT", 60.0)))

    conn = _ORIG_CONNECT(database, *args, **kwargs)
    try:
        _apply_pragmas(conn, database)
    except Exception:
        try:
            logger.debug("[SQLITE MEMORY PRAGMAS] apply failed database=%s", database, exc_info=True)
        except Exception:
            pass
    return conn


def _install_summary_spool_flush_patch() -> bool:
    """Patch spool flush to use the robust bulk upsert path.

    The old direct DELETE+INSERT path can fail and keep failed_files forever when the spool
    payload has newer columns or pandas/numpy scalar types.  The bulk path already normalizes
    columns, handles latest_only, owner guard, and retry IO patches.
    """
    global _SUMMARY_SPOOL_FLUSH_PATCHED
    if _SUMMARY_SPOOL_FLUSH_PATCHED:
        return True
    if _env_bool("DISABLE_SUMMARY_SPOOL_FLUSH_BULK_PATCH", False):
        return False

    try:
        import trading.summary.persistence.summary_save_spool as spool
        import trading.summary.persistence.summary_saver_bulk as saver

        old = getattr(spool, "_save_direct", None)
        if not callable(old):
            return False
        if getattr(old, "_summary_spool_flush_bulk_patch", False):
            _SUMMARY_SPOOL_FLUSH_PATCHED = True
            return True

        def _patched_save_direct(df: Any, *, interval: int, source: str, date_yyyymmdd: str) -> int:
            try:
                # Ranking column repair now runs unconditionally inside
                # summary_saver_bulk._get_table_columns_from_engine itself.
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
        _SUMMARY_SPOOL_FLUSH_PATCHED = True
        logger.warning("[SUMMARY SAVE SPOOL PATCH] installed bulk_flush=True")
        return True
    except Exception:
        logger.exception("[SUMMARY SAVE SPOOL PATCH] install failed")
        return False


def install() -> bool:
    global _INSTALLED

    _force_summary_lock_env()
    spool_ok = _install_summary_spool_flush_patch()

    if _INSTALLED:
        return True
    if not _env_bool("SQLITE_MEMORY_PRAGMAS_ENABLED", True):
        return bool(spool_ok)
    try:
        sqlite3.connect = _patched_connect  # type: ignore[assignment]
        _INSTALLED = True
        logger.warning(
            "[SQLITE MEMORY PRAGMAS] installed temp_store=%s cache_kb=%s mmap=%s spill_off=%s summary_busy_ms=%s spool_bulk=%s",
            os.getenv("SQLITE_MEMORY_TEMP_STORE", "MEMORY"),
            os.getenv("SQLITE_MEMORY_CACHE_KB", "-65536"),
            os.getenv("SQLITE_MMAP_SIZE_BYTES", "268435456"),
            os.getenv("SQLITE_CACHE_SPILL_OFF", "1"),
            os.getenv("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", "60000"),
            spool_ok,
        )
        return True
    except Exception:
        logger.exception("[SQLITE MEMORY PRAGMAS] install failed")
        return bool(spool_ok)
