# ============================================================
# File   : core/startup/sqlite_memory_pragmas_patch.py
# Version: SQLITE-MEMORY-PRAGMAS-PATCH-V3-SUMMARY-RANKING-SPOOL
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
_RANKING_LEGACY_SCHEMA_PATCHED = False
_SUMMARY_RANKING_SCHEMA_PATCHED = False
_SUMMARY_SPOOL_FLUSH_PATCHED = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}


LEGACY_RANKING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("symbol", "TEXT"),
    ("symbolname", "TEXT"),
    ("current_price", "REAL"),
    ("change_percentage", "REAL"),
    ("change_ratio", "REAL"),
    ("trading_volume", "REAL"),
    ("trading_value", "REAL"),
    ("turnover", "REAL"),
    ("tick_count", "INTEGER"),
    ("inserted_at", "TEXT"),
    ("rank", "INTEGER"),
)


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

    try:
        _ensure_summary_ranking_columns_on_connection(conn, database)
    except Exception:
        pass


def _patched_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    conn = _ORIG_CONNECT(database, *args, **kwargs)
    try:
        _apply_pragmas(conn, database)
    except Exception:
        try:
            logger.debug("[SQLITE MEMORY PRAGMAS] apply failed database=%s", database, exc_info=True)
        except Exception:
            pass
    return conn


def _install_ranking_legacy_schema_patch() -> bool:
    """Patch RankingDBWriter legacy table ensure for existing old DB files.

    ranking_db_writer.py の CREATE TABLE IF NOT EXISTS は既存テーブルの列不足を
    補修しないため、古い ranking DB では INSERT rank で落ちる。
    ここで _ensure_legacy_table を包み、CREATE 後に PRAGMA table_info -> ALTER TABLE
    を実行して不足列を自動追加する。
    """
    global _RANKING_LEGACY_SCHEMA_PATCHED
    if _RANKING_LEGACY_SCHEMA_PATCHED:
        return True
    if _env_bool("DISABLE_RANKING_LEGACY_SCHEMA_REPAIR_PATCH", False):
        return False

    try:
        import trading.ranking.ranking_db_writer as writer_mod

        cls = getattr(writer_mod, "RankingDBWriter", None)
        old = getattr(cls, "_ensure_legacy_table", None) if cls is not None else None
        if cls is None or not callable(old):
            return False
        if getattr(old, "_ranking_legacy_schema_repair_patch", False):
            _RANKING_LEGACY_SCHEMA_PATCHED = True
            return True

        def _patched_ensure_legacy_table(self: Any, table: str) -> None:
            old(self, table)
            cur = getattr(self, "cursor", None)
            if cur is None:
                return
            _ensure_columns_with_cursor(cur, table, LEGACY_RANKING_COLUMNS, "RANKING LEGACY SCHEMA REPAIR")

        _patched_ensure_legacy_table._ranking_legacy_schema_repair_patch = True  # type: ignore[attr-defined]
        _patched_ensure_legacy_table._original = old  # type: ignore[attr-defined]
        cls._ensure_legacy_table = _patched_ensure_legacy_table
        _RANKING_LEGACY_SCHEMA_PATCHED = True
        logger.warning("[RANKING LEGACY SCHEMA REPAIR] installed")
        return True
    except Exception:
        logger.exception("[RANKING LEGACY SCHEMA REPAIR] install failed")
        return False


def _install_summary_ranking_schema_patch() -> bool:
    """Patch summary_saver_bulk table column discovery so ranking columns are not dropped.

    summary_saver_bulk aligns DataFrame columns to the physical SQLite table.  When old
    summary DB files do not yet have ranking columns, rank/ranking_score/ranking_type are
    dropped before upsert.  This wrapper repairs the table before column discovery returns.
    """
    global _SUMMARY_RANKING_SCHEMA_PATCHED
    if _SUMMARY_RANKING_SCHEMA_PATCHED:
        return True
    if _env_bool("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", False):
        return False

    try:
        import trading.summary.persistence.summary_saver_bulk as saver

        old = getattr(saver, "_get_table_columns_from_engine", None)
        if not callable(old):
            return False
        if getattr(old, "_summary_ranking_schema_repair_patch", False):
            _SUMMARY_RANKING_SCHEMA_PATCHED = True
            return True

        def _patched_get_table_columns_from_engine(engine: Any, table_name: str):
            if engine is not None and str(table_name) in SUMMARY_TABLES:
                try:
                    with engine.begin() as conn:
                        rows = conn.exec_driver_sql(f"PRAGMA table_info({_quote_ident(str(table_name))})").fetchall()
                        if rows:
                            existing = {
                                str(r[1]).strip()
                                for r in rows
                                if len(r) > 1 and r[1] is not None and str(r[1]).strip()
                            }
                            added: list[str] = []
                            for col, decl in SUMMARY_RANKING_COLUMNS:
                                if col in existing:
                                    continue
                                try:
                                    conn.exec_driver_sql(
                                        f"ALTER TABLE {_quote_ident(str(table_name))} ADD COLUMN {_quote_ident(col)} {decl}"
                                    )
                                    existing.add(col)
                                    added.append(col)
                                except Exception as e:
                                    msg = str(e).lower()
                                    if "duplicate column" in msg or "already exists" in msg:
                                        existing.add(col)
                                        continue
                                    raise
                            if added:
                                logger.warning(
                                    "[SUMMARY RANKING SCHEMA REPAIR] table=%s added_columns=%s",
                                    table_name,
                                    added,
                                )
                except Exception:
                    logger.debug("[SUMMARY RANKING SCHEMA REPAIR] engine repair failed table=%s", table_name, exc_info=True)
            return old(engine, table_name)

        _patched_get_table_columns_from_engine._summary_ranking_schema_repair_patch = True  # type: ignore[attr-defined]
        _patched_get_table_columns_from_engine._original = old  # type: ignore[attr-defined]
        saver._get_table_columns_from_engine = _patched_get_table_columns_from_engine
        _SUMMARY_RANKING_SCHEMA_PATCHED = True
        logger.warning("[SUMMARY RANKING SCHEMA REPAIR] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY RANKING SCHEMA REPAIR] install failed")
        return False


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
                # Ensure summary tables have ranking columns before bulk alignment.
                _install_summary_ranking_schema_patch()
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

    ranking_schema_ok = _install_ranking_legacy_schema_patch()
    summary_schema_ok = _install_summary_ranking_schema_patch()
    spool_ok = _install_summary_spool_flush_patch()

    if _INSTALLED:
        return True
    if not _env_bool("SQLITE_MEMORY_PRAGMAS_ENABLED", True):
        return bool(ranking_schema_ok or summary_schema_ok or spool_ok)
    try:
        sqlite3.connect = _patched_connect  # type: ignore[assignment]
        _INSTALLED = True
        logger.warning(
            "[SQLITE MEMORY PRAGMAS] installed temp_store=%s cache_kb=%s mmap=%s spill_off=%s ranking_legacy_schema=%s summary_ranking_schema=%s spool_bulk=%s",
            os.getenv("SQLITE_MEMORY_TEMP_STORE", "MEMORY"),
            os.getenv("SQLITE_MEMORY_CACHE_KB", "-65536"),
            os.getenv("SQLITE_MMAP_SIZE_BYTES", "268435456"),
            os.getenv("SQLITE_CACHE_SPILL_OFF", "1"),
            ranking_schema_ok,
            summary_schema_ok,
            spool_ok,
        )
        return True
    except Exception:
        logger.exception("[SQLITE MEMORY PRAGMAS] install failed")
        return bool(ranking_schema_ok or summary_schema_ok or spool_ok)
