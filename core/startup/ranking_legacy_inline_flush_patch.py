# -*- coding: utf-8 -*-
"""
Fast legacy-only flush for ranking writer.

The V6 ranking writer lock patch makes raw/snapshot flush fast, but legacy rows can
be queued just after that flush and then hit `already_running` once.  This patch
adds a small legacy-only fast path so the remaining legacy buffer is flushed by a
dedicated SQLite connection without falling back to the slower original writer.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-RANKING-LEGACY-INLINE-FLUSH"
_INSTALLED = False


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(float(str(os.getenv(name, str(default))).replace(",", "")))
    except Exception:
        v = int(default)
    if min_value is not None:
        v = max(v, min_value)
    if max_value is not None:
        v = min(v, max_value)
    return int(v)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = str(os.getenv(name, "")).strip().lower()
        if v in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if v in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _chunks(items: list[Any], size: int):
    size = max(1, int(size or 1000))
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _is_locked_error(err: Any) -> bool:
    s = str(err or "").lower()
    return "database is locked" in s or "database table is locked" in s or "locked" in s


def _resolve_db_path(writer: Any, target: Any) -> Path:
    try:
        dbp = getattr(writer, "db_path", None)
        if dbp:
            return Path(dbp)
    except Exception:
        pass
    try:
        return Path(target._resolve_ranking_db_path())
    except Exception:
        from database.paths.ranking_paths import get_ranking_db_path
        return Path(get_ranking_db_path())


def _exec_and_drain(cur: sqlite3.Cursor, sql: str) -> None:
    cur.execute(sql)
    try:
        cur.fetchall()
    except Exception:
        pass


def _open_conn(db_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=max(1.0, busy_timeout_ms / 1000.0), check_same_thread=False)
    cur = conn.cursor()
    try:
        _exec_and_drain(cur, f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        _exec_and_drain(cur, "PRAGMA synchronous=NORMAL;")
        _exec_and_drain(cur, "PRAGMA temp_store=MEMORY;")
        try:
            _exec_and_drain(cur, f"PRAGMA cache_size={_env_int('RANKING_LEGACY_FLUSH_CACHE_SIZE', -32768)};")
        except Exception:
            pass
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return conn


def _quote(target: Any, name: str) -> str:
    try:
        return target.quote_ident(name)
    except Exception:
        return '"' + str(name).replace('"', '""') + '"'


def _ensure_legacy_table(cur: sqlite3.Cursor, target: Any, table: str) -> None:
    q = _quote(target, table)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {q} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        symbolname TEXT,
        current_price REAL,
        change_percentage REAL,
        change_ratio REAL,
        trading_volume REAL,
        trading_value REAL,
        turnover REAL,
        tick_count INTEGER,
        inserted_at TEXT,
        rank INTEGER
    )
    """)
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {_quote(target, 'idx_' + table + '_inserted_at')} "
        f"ON {q}(inserted_at)"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {_quote(target, 'idx_' + table + '_symbol_inserted_at')} "
        f"ON {q}(symbol, inserted_at)"
    )


def _flush_legacy_only(writer: Any, target: Any, legacy_rows: list[dict]) -> bool:
    if not legacy_rows:
        return True

    t0 = time.time()
    busy_timeout_ms = _env_int("RANKING_LEGACY_FLUSH_BUSY_TIMEOUT_MS", 5000, min_value=500, max_value=60000)
    batch_size = _env_int("RANKING_LEGACY_FLUSH_BATCH_SIZE", 1000, min_value=50, max_value=5000)
    retry_max = _env_int("RANKING_LEGACY_FLUSH_RETRY_MAX", 1, min_value=0, max_value=5)
    db_path = _resolve_db_path(writer, target)
    saved = 0
    last_error = ""

    for attempt in range(retry_max + 1):
        conn: sqlite3.Connection | None = None
        cur: sqlite3.Cursor | None = None
        try:
            conn = _open_conn(db_path, busy_timeout_ms)
            cur = conn.cursor()
            conn.execute("BEGIN IMMEDIATE")

            grouped: dict[tuple[str, str], list[dict]] = {}
            for row in legacy_rows:
                try:
                    x = target._normalize_for_legacy(row)
                    key = (str(x.get("ranking_type") or "UNKNOWN"), str(x.get("market") or "ALL"))
                    grouped.setdefault(key, []).append(row)
                except Exception:
                    logger.debug("[RANKING LEGACY INLINE FLUSH] group skipped row=%r", row, exc_info=True)

            for (ranking_type, market), rows in grouped.items():
                table = target._legacy_table_name(ranking_type, market)
                _ensure_legacy_table(cur, target, table)
                q = _quote(target, table)
                sql = f"""
                INSERT INTO {q} (
                    symbol,
                    symbolname,
                    current_price,
                    change_percentage,
                    change_ratio,
                    trading_volume,
                    trading_value,
                    turnover,
                    tick_count,
                    inserted_at,
                    rank
                ) VALUES ({','.join(['?'] * 11)})
                """
                params = [writer._legacy_tuple(r) for r in rows]
                for part in _chunks(params, batch_size):
                    cur.executemany(sql, part)
                    saved += len(part)

            try:
                cur.close()
            finally:
                cur = None
            conn.commit()

            elapsed = time.time() - t0
            now_iso = dt.datetime.now().isoformat(timespec="seconds")
            try:
                with writer.lock:
                    writer.total_flushed_legacy += saved
                    writer.last_flush_at = now_iso
                    writer.last_flush_elapsed_sec = float(elapsed)
                    writer.last_error = None
                    writer._mark_runtime()
            except Exception:
                pass

            logger.warning(
                "[RANKING LEGACY INLINE FLUSH] flush done legacy=%d input_legacy=%d elapsed=%.3fs db=%s",
                saved,
                len(legacy_rows),
                elapsed,
                db_path,
            )
            return True

        except sqlite3.OperationalError as e:
            last_error = str(e)
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            if not _is_locked_error(e) or attempt >= retry_max:
                break
            sleep_sec = min(0.8, 0.2 * (2 ** attempt))
            logger.warning(
                "[RANKING LEGACY INLINE FLUSH] retry attempt=%s/%s sleep=%.2fs rows=%d err=%s",
                attempt + 1,
                retry_max,
                sleep_sec,
                len(legacy_rows),
                e,
            )
            time.sleep(sleep_sec)
        except Exception as e:
            last_error = str(e)
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            logger.exception("[RANKING LEGACY INLINE FLUSH] failed")
            break
        finally:
            try:
                if cur is not None:
                    cur.close()
            except Exception:
                pass
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    try:
        with writer.lock:
            writer.legacy_buffer = list(legacy_rows) + list(getattr(writer, "legacy_buffer", []) or [])
            writer.total_flush_errors += 1
            writer.last_error = last_error or "legacy inline flush failed"
            writer.last_flush_elapsed_sec = time.time() - t0
            writer._mark_runtime()
    except Exception:
        pass
    logger.warning(
        "[RANKING LEGACY INLINE FLUSH] returned rows to buffer legacy=%d err=%s",
        len(legacy_rows),
        last_error,
    )
    return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if _env_bool("DISABLE_RANKING_LEGACY_INLINE_FLUSH_PATCH", False):
        logger.warning("[RANKING LEGACY INLINE FLUSH] disabled by env")
        return False
    try:
        import trading.ranking.ranking_db_writer as target
    except Exception:
        logger.debug("[RANKING LEGACY INLINE FLUSH] ranking_db_writer not importable yet", exc_info=True)
        return False

    cls = getattr(target, "RankingDBWriter", None)
    if cls is None:
        return False
    if getattr(cls, "_legacy_inline_flush_patch_installed", False):
        _INSTALLED = True
        return True

    old_flush = cls.flush

    def flush_patched(self: Any, *args: Any, **kwargs: Any) -> bool:
        # If raw/snapshot exists, let the existing V6/raw path handle it.
        # This patch is for the common leftover state: legacy_buffer only.
        try:
            with self.lock:
                raw_has = bool(getattr(self, "raw_buffer", None))
                snapshot_has = bool(getattr(self, "snapshot_buffer", None))
                legacy_has = bool(getattr(self, "legacy_buffer", None))
                running = bool(getattr(self, "_flush_v6_running", False) or getattr(self, "_legacy_inline_running", False))
                if raw_has or snapshot_has or not legacy_has:
                    return old_flush(self, *args, **kwargs)
                if running:
                    logger.warning(
                        "[RANKING LEGACY INLINE FLUSH] skipped reason=already_running legacy_buffer=%d",
                        len(getattr(self, "legacy_buffer", []) or []),
                    )
                    return False
                self._legacy_inline_running = True
                legacy_rows = list(getattr(self, "legacy_buffer", []) or [])
                self.legacy_buffer = []
                try:
                    self._mark_runtime()
                except Exception:
                    pass
        except Exception:
            return old_flush(self, *args, **kwargs)

        try:
            return _flush_legacy_only(self, target, legacy_rows)
        finally:
            try:
                with self.lock:
                    self._legacy_inline_running = False
            except Exception:
                try:
                    self._legacy_inline_running = False
                except Exception:
                    pass

    cls.flush = flush_patched
    cls._legacy_inline_flush_patch_installed = True
    _INSTALLED = True
    logger.warning("[RANKING LEGACY INLINE FLUSH] installed version=%s", VERSION)
    return True


__all__ = ["VERSION", "install"]
