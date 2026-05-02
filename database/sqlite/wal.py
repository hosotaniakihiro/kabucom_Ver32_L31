# ============================================================
# File   : database/sqlite/wal.py
# Version: PRODUCTION-STABLE-REV1.0-SQLITE-WAL-CHECKPOINT
# ------------------------------------------------------------
# Purpose:
#   SQLite WAL checkpoint / WALファイル管理を共通化する。
#
# Notes:
#   - .db-wal が増えて .db 本体が増えないのはWALでは正常
#   - PASSIVE は通常運用向け
#   - TRUNCATE は昼休み・大引け後など低負荷時向け
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

CheckpointMode = Literal["PASSIVE", "FULL", "RESTART", "TRUNCATE"]


def get_wal_path(db_path: str | Path) -> Path:
    """
    SQLite DBに対応する .db-wal パスを返す。
    """
    return Path(str(db_path) + "-wal")


def get_shm_path(db_path: str | Path) -> Path:
    """
    SQLite DBに対応する .db-shm パスを返す。
    """
    return Path(str(db_path) + "-shm")


def get_file_size_bytes(path: str | Path) -> int:
    """
    ファイルサイズを返す。存在しない場合は0。
    """
    try:
        p = Path(path)
        if not p.exists():
            return 0
        return int(p.stat().st_size)
    except Exception:
        return 0


def get_sqlite_wal_status(db_path: str | Path) -> dict:
    """
    DB / WAL / SHM の存在とサイズを返す。
    """
    db = Path(db_path)
    wal = get_wal_path(db)
    shm = get_shm_path(db)

    return {
        "db_path": str(db),
        "wal_path": str(wal),
        "shm_path": str(shm),
        "db_exists": db.exists(),
        "wal_exists": wal.exists(),
        "shm_exists": shm.exists(),
        "db_size_bytes": get_file_size_bytes(db),
        "wal_size_bytes": get_file_size_bytes(wal),
        "shm_size_bytes": get_file_size_bytes(shm),
    }


def checkpoint_sqlite_wal(
    db_path: str | Path,
    *,
    mode: CheckpointMode = "PASSIVE",
    timeout: float = 5.0,
    busy_timeout_ms: int = 5000,
) -> bool:
    """
    SQLite WAL checkpoint を実行する。

    Parameters
    ----------
    db_path:
        対象SQLite DBパス。

    mode:
        PASSIVE:
            通常運用向け。ロック競合が少ない。
        FULL:
            可能なら強めにcheckpoint。
        RESTART:
            readerがいなければWAL再開始。
        TRUNCATE:
            WALを縮小。取引中の多用は非推奨。

    Returns
    -------
    bool
        checkpoint実行成功なら True。
    """
    path = Path(db_path)

    if not path.exists():
        logger.warning("[SQLITE WAL] db not found path=%s", path)
        return False

    mode = str(mode or "PASSIVE").upper()
    if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        mode = "PASSIVE"

    before = get_sqlite_wal_status(path)

    try:
        with sqlite3.connect(str(path), timeout=float(timeout)) as conn:
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            conn.execute("PRAGMA synchronous=NORMAL")
            result = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchall()

        after = get_sqlite_wal_status(path)

        logger.info(
            "[SQLITE WAL] checkpoint done db=%s mode=%s result=%s wal_before=%s wal_after=%s db_before=%s db_after=%s",
            path,
            mode,
            result,
            before.get("wal_size_bytes"),
            after.get("wal_size_bytes"),
            before.get("db_size_bytes"),
            after.get("db_size_bytes"),
        )
        return True

    except Exception:
        logger.exception("[SQLITE WAL] checkpoint failed db=%s mode=%s", path, mode)
        return False


def checkpoint_sqlite_wal_if_large(
    db_path: str | Path,
    *,
    threshold_mb: float = 256.0,
    mode: CheckpointMode = "PASSIVE",
    timeout: float = 5.0,
    busy_timeout_ms: int = 5000,
) -> bool:
    """
    WALファイルが一定サイズ以上なら checkpoint する。

    毎分ジョブでは PASSIVE 推奨。
    TRUNCATE は昼休み・大引け後だけ推奨。
    """
    status = get_sqlite_wal_status(db_path)
    wal_size = int(status.get("wal_size_bytes") or 0)
    threshold_bytes = int(float(threshold_mb) * 1024 * 1024)

    if wal_size < threshold_bytes:
        logger.debug(
            "[SQLITE WAL] checkpoint skipped wal small db=%s wal=%s threshold=%s",
            db_path,
            wal_size,
            threshold_bytes,
        )
        return False

    logger.info(
        "[SQLITE WAL] checkpoint triggered db=%s wal=%s threshold=%s mode=%s",
        db_path,
        wal_size,
        threshold_bytes,
        mode,
    )

    return checkpoint_sqlite_wal(
        db_path,
        mode=mode,
        timeout=timeout,
        busy_timeout_ms=busy_timeout_ms,
    )


__all__ = [
    "CheckpointMode",
    "get_wal_path",
    "get_shm_path",
    "get_file_size_bytes",
    "get_sqlite_wal_status",
    "checkpoint_sqlite_wal",
    "checkpoint_sqlite_wal_if_large",
]