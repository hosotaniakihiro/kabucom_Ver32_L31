from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_INSTALLED = False
_THREAD: threading.Thread | None = None
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}
_MODES = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = str(os.getenv(name, "")).strip()
        return float(raw) if raw else float(default)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = str(os.getenv(name, "")).strip()
        return int(float(raw)) if raw else int(default)
    except Exception:
        return int(default)


def _mode() -> str:
    mode = str(os.getenv("SUMMARY_WAL_CHECKPOINT_MODE", "PASSIVE")).strip().upper()
    return mode if mode in _MODES else "PASSIVE"


def _summary_db_path() -> Path:
    from data_collectors.config import summary_db_path
    return Path(summary_db_path())


def _checkpoint_once() -> bool:
    db_path = _summary_db_path()
    if not db_path.exists():
        logger.debug("[SUMMARY WAL CHECKPOINT] skip db missing path=%s", db_path)
        return False

    mode = _mode()
    busy_timeout_ms = max(1000, _env_int("SUMMARY_WAL_CHECKPOINT_BUSY_TIMEOUT_MS", 5000))
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
            mode,
            db_path,
            wal_size,
            result,
        )
        return True
    except sqlite3.OperationalError as exc:
        logger.warning(
            "[SUMMARY WAL CHECKPOINT] skip busy mode=%s db=%s err=%s",
            mode,
            db_path,
            exc,
        )
        return False
    except Exception:
        logger.exception("[SUMMARY WAL CHECKPOINT] failed db=%s", db_path)
        return False


def _loop() -> None:
    initial_delay = max(0.0, _env_float("SUMMARY_WAL_CHECKPOINT_INITIAL_DELAY_SEC", 20.0))
    interval = max(10.0, _env_float("SUMMARY_WAL_CHECKPOINT_INTERVAL_SEC", 60.0))
    if initial_delay:
        time.sleep(initial_delay)
    while True:
        if _env_bool("SUMMARY_WAL_CHECKPOINT_ENABLED", True):
            _checkpoint_once()
        time.sleep(interval)


def install() -> bool:
    global _INSTALLED, _THREAD
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_WAL_CHECKPOINT_ENABLED", True):
        logger.warning("[SUMMARY WAL CHECKPOINT] disabled by env")
        return False

    os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_INTERVAL_SEC", "60")
    os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_MODE", "PASSIVE")
    os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_BUSY_TIMEOUT_MS", "5000")
    os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_INITIAL_DELAY_SEC", "20")

    _THREAD = threading.Thread(target=_loop, name="summary-wal-checkpoint-loop", daemon=True)
    _THREAD.start()
    _INSTALLED = True
    logger.warning(
        "[SUMMARY WAL CHECKPOINT] installed interval=%ss mode=%s",
        os.getenv("SUMMARY_WAL_CHECKPOINT_INTERVAL_SEC"),
        _mode(),
    )
    return True
