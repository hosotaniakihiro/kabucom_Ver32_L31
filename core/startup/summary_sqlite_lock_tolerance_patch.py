# ============================================================
# File   : core/startup/summary_sqlite_lock_tolerance_patch.py
# Version: REV1-SUMMARY-SQLITE-LOCK-TOLERANCE
# ------------------------------------------------------------
# main_database.py / summary_database_runner 側の summary DB 書き込みで
# database is locked が出やすい箇所の共通耐性を上げる。
#
# 目的:
#   - sqlite3.connect の timeout を summary DB では最低60秒へ引き上げ
#   - 接続直後に busy_timeout / WAL / synchronous=NORMAL を適用
#   - MTF catchup / indicator fill の小分け・retry系 ENV を安全側に寄せる
#   - 既存の保存ロジック自体は変更しない
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_CONNECT = sqlite3.connect

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _TRUE:
            return True
        if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _looks_like_summary_db(database: Any) -> bool:
    try:
        s = str(database or "")
        name = Path(s).name.lower()
        return name.startswith("summary") and name.endswith(".db")
    except Exception:
        return False


def _setdefault_env(name: str, value: str) -> None:
    try:
        if not str(os.getenv(name, "")).strip():
            os.environ[name] = value
    except Exception:
        pass


def _force_summary_lock_env() -> None:
    # SQLite共通。NAS SQLiteでは5秒だと短すぎるため、summary系だけ実質60秒へ寄せる。
    _setdefault_env("SQLITE_BUSY_TIMEOUT_MS", "60000")
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


def _configure_summary_connection(conn: sqlite3.Connection) -> None:
    pragmas = [
        "PRAGMA busy_timeout=60000",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA temp_store=MEMORY",
        "PRAGMA cache_size=-131072",
        "PRAGMA wal_autocheckpoint=1000",
    ]
    for sql in pragmas:
        try:
            conn.execute(sql)
        except Exception:
            # journal_modeなどはタイミングにより失敗しても致命傷にしない。
            pass


def _patched_connect(database: Any, *args: Any, **kwargs: Any):
    is_summary = _looks_like_summary_db(database)
    if is_summary:
        try:
            current_timeout = float(kwargs.get("timeout", 0) or 0)
        except Exception:
            current_timeout = 0.0
        kwargs["timeout"] = max(current_timeout, 60.0)

    conn = _ORIGINAL_CONNECT(database, *args, **kwargs)

    if is_summary:
        try:
            _configure_summary_connection(conn)
        except Exception:
            logger.debug("[SUMMARY SQLITE LOCK PATCH] configure skipped database=%s", database, exc_info=True)

    return conn


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_SQLITE_LOCK_TOLERANCE_PATCH", True):
        logger.warning("[SUMMARY SQLITE LOCK PATCH] disabled by env")
        return False

    _force_summary_lock_env()
    sqlite3.connect = _patched_connect  # type: ignore[assignment]
    _INSTALLED = True
    logger.warning(
        "[SUMMARY SQLITE LOCK PATCH] installed version=REV1 summary_timeout=60s busy_timeout=60000ms "
        "indicator_chunk=%s indicator_retries=%s catchup_chunk=%s catchup_retries=%s",
        os.getenv("SUMMARY_MTF_INDICATOR_UPDATE_CHUNK_SIZE"),
        os.getenv("SUMMARY_MTF_INDICATOR_LOCK_RETRIES"),
        os.getenv("SUMMARY_MTF_CATCHUP_UPSERT_CHUNK_SIZE"),
        os.getenv("SUMMARY_MTF_CATCHUP_UPSERT_RETRIES"),
    )
    return True
