# ============================================================
# File   : core/startup/sqlite_memory_pragmas_patch.py
# Version: SQLITE-MEMORY-PRAGMAS-PATCH-V1
# ------------------------------------------------------------
# Purpose:
#   main_database.py / data collector 子プロセスの SQLite 接続に対して、
#   メモリに余裕がある環境向けの PRAGMA を自動適用する。
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
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}


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


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SQLITE_MEMORY_PRAGMAS_ENABLED", True):
        return False
    try:
        sqlite3.connect = _patched_connect  # type: ignore[assignment]
        _INSTALLED = True
        logger.warning(
            "[SQLITE MEMORY PRAGMAS] installed temp_store=%s cache_kb=%s mmap=%s spill_off=%s",
            os.getenv("SQLITE_MEMORY_TEMP_STORE", "MEMORY"),
            os.getenv("SQLITE_MEMORY_CACHE_KB", "-65536"),
            os.getenv("SQLITE_MMAP_SIZE_BYTES", "268435456"),
            os.getenv("SQLITE_CACHE_SPILL_OFF", "1"),
        )
        return True
    except Exception:
        logger.exception("[SQLITE MEMORY PRAGMAS] install failed")
        return False
