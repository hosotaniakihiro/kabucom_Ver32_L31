# ============================================================
# File   : trading/ranking/ranking_db_writer_lock_patch.py
# Version: PRODUCTION-STABLE-RANKING-DB-WRITER-LOCK-PATCH-V4-FINALIZE-BEFORE-COMMIT
# ------------------------------------------------------------
# Purpose:
#   ranking_db_writer.py 本体を大きく壊さず、SQLite locked / cursor再入 / commit時未完了SQL 対策を後付けする。
#
# Fixes:
#   - sqlite3.ProgrammingError: Recursive use of cursors not allowed.
#   - sqlite3.OperationalError: cannot commit transaction - SQL statements in progress
#
# V4:
#   - flush 専用 local cursor を使い、self.cursor を flush では使わない
#   - PRAGMA の戻り結果を必ず fetchall して statement を完了させる
#   - executemany 後、commit 前に local cursor を close して statement を finalize する
#   - flush 再入中は新しい flush を skip して buffer を保持
#   - legacy_buffer が無い通常運用では DELETE を使わず INSERT OR REPLACE
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(default) if v is None or str(v).strip() == "" else int(v)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(default) if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return float(default)


def _is_locked_error(err: Any) -> bool:
    s = str(err or "").lower()
    return "database is locked" in s or "database table is locked" in s or "locked" in s


def _is_statement_progress_error(err: Any) -> bool:
    s = str(err or "").lower()
    return "sql statements in progress" in s or "statements in progress" in s


def _buffer_count(writer: Any) -> int:
    try:
        return (
            len(getattr(writer, "raw_buffer", []) or [])
            + len(getattr(writer, "snapshot_buffer", []) or [])
            + len(getattr(writer, "legacy_buffer", []) or [])
        )
    except Exception:
        return 0


def _chunks(items: list[Any], size: int):
    size = max(1, int(size or 500))
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _type_counts(target: Any, rows: list[dict]) -> dict[str, int]:
    try:
        return target._type_counts(rows)
    except Exception:
        return {}


def _return_to_front(writer: Any, raw_rows: list[dict], snapshot_rows: list[dict], legacy_rows: list[dict]) -> None:
    try:
        with writer.lock:
            writer.raw_buffer = list(raw_rows or []) + list(getattr(writer, "raw_buffer", []) or [])
            writer.snapshot_buffer = list(snapshot_rows or []) + list(getattr(writer, "snapshot_buffer", []) or [])
            writer.legacy_buffer = list(legacy_rows or []) + list(getattr(writer, "legacy_buffer", []) or [])
            try:
                writer._mark_runtime()
            except Exception:
                pass
    except Exception:
        logger.exception("[RANKING DB WRITER LOCK PATCH] return buffer failed")


def _mark_flush_error(writer: Any, error: str, started_at: float) -> None:
    try:
        writer.total_flush_errors += 1
        writer.last_error = error
        writer.last_flush_elapsed_sec = time.time() - started_at
        writer._mark_runtime()
    except Exception:
        pass


def _exec_and_drain(cur: sqlite3.Cursor, sql: str) -> None:
    """Execute SQL and drain result rows if the statement returns any.

    PRAGMA journal_mode returns a row; leaving it undrained can make sqlite3
    raise 'cannot commit transaction - SQL statements in progress'.
    """
    cur.execute(sql)
    try:
        cur.fetchall()
    except Exception:
        pass


def install_ranking_db_writer_lock_patch() -> bool:
    try:
        from trading.ranking import ranking_db_writer as target
    except Exception:
        logger.exception("[RANKING DB WRITER LOCK PATCH] import target failed")
        return False

    cls = getattr(target, "RankingDBWriter", None)
    if cls is None:
        logger.warning("[RANKING DB WRITER LOCK PATCH] RankingDBWriter not found")
        return False

    if getattr(cls, "_lock_retry_patch_v4_installed", False):
        return True

    busy_timeout_ms = _env_int("RANKING_WRITER_BUSY_TIMEOUT_MS", 30000)
    retry_max = _env_int("RANKING_WRITER_LOCK_RETRY_MAX", 5)
    retry_base_sec = _env_float("RANKING_WRITER_LOCK_RETRY_BASE_SEC", 0.5)
    retry_max_sleep_sec = _env_float("RANKING_WRITER_LOCK_RETRY_MAX_SLEEP_SEC", 3.0)
    batch_size = _env_int("RANKING_WRITER_SQL_BATCH_SIZE", 300)

    try:
        target.DEFAULT_BUSY_TIMEOUT_MS = int(busy_timeout_ms)
    except Exception:
        pass

    orig_flush = cls.flush

    def flush_replace_upsert(self, *args, **kwargs) -> bool:
        with self.lock:
            if getattr(self, "_flush_v4_running", False):
                logger.warning("[RANKING DB WRITER LOCK PATCH] flush skipped reason=already_running buffer=%s", _buffer_count(self))
                return False
            if not (self.raw_buffer or self.snapshot_buffer or self.legacy_buffer):
                logger.debug("[RANKING DB WRITER] flush skipped empty")
                return True
            self._flush_v4_running = True
            raw_rows = list(self.raw_buffer or [])
            snapshot_rows = list(self.snapshot_buffer or [])
            legacy_rows = list(self.legacy_buffer or [])
            self.raw_buffer = []
            self.snapshot_buffer = []
            self.legacy_buffer = []

        try:
            if legacy_rows:
                _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                return bool(orig_flush(self, *args, **kwargs))

            t0 = time.time()
            last_error = ""

            snapshot_insert_sql = f"""
            INSERT OR REPLACE INTO {target.quote_ident(target.SNAPSHOT_TABLE)} (
                symbol, datetime, snapshot_time, symbolname, current_price, price,
                change_percentage, change_rate, trading_volume, volume, trading_value,
                turnover, tick_count, ranking_type, rank_type, category, market,
                exchange, source, rank, created_at, inserted_at
            ) VALUES ({','.join(['?'] * 22)})
            """

            raw_insert_sql = f"""
            INSERT OR IGNORE INTO {target.quote_ident(target.RAW_TABLE)} (
                ingest_id, symbol, datetime, snapshot_time, symbolname, current_price,
                price, change_percentage, change_rate, change_ratio, trading_volume,
                volume, trading_value, turnover, tick_count, ranking_type, rank_type,
                category, market, exchange, source, rank, date, time, raw_json,
                received_at, created_at, inserted_at, updated_at
            ) VALUES ({','.join(['?'] * 29)})
            """

            snapshot_params: list[tuple] = []
            for row in snapshot_rows:
                try:
                    snapshot_params.append(target.normalize_snapshot_row(row))
                except Exception:
                    logger.warning("[RANKING DB WRITER] snapshot normalize skipped row=%r", row, exc_info=True)

            raw_params: list[tuple] = []
            for row in raw_rows:
                try:
                    raw_params.append(target.normalize_raw_row(row))
                except Exception:
                    logger.warning("[RANKING DB WRITER] raw normalize skipped row=%r", row, exc_info=True)

            for attempt in range(int(retry_max) + 1):
                saved_snapshot = 0
                saved_raw = 0
                cur: sqlite3.Cursor | None = None
                committed = False
                try:
                    with self.lock:
                        self._ensure_connection()
                        assert self.conn is not None

                        cur = self.conn.cursor()
                        _exec_and_drain(cur, f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
                        # journal_mode can return rows; drain it. If another statement is active, keep current mode.
                        try:
                            _exec_and_drain(cur, "PRAGMA journal_mode=WAL;")
                        except sqlite3.OperationalError:
                            logger.debug("[RANKING DB WRITER LOCK PATCH] journal_mode pragma skipped", exc_info=True)
                        _exec_and_drain(cur, "PRAGMA synchronous=NORMAL;")
                        _exec_and_drain(cur, "PRAGMA temp_store=MEMORY;")

                        logger.info(
                            "[RANKING DB WRITER] flush prepare v4 finalize-before-commit raw=%d snapshot=%d legacy=%d snapshot_types=%s raw_types=%s batch_size=%d",
                            len(raw_rows), len(snapshot_rows), len(legacy_rows),
                            _type_counts(target, snapshot_rows), _type_counts(target, raw_rows), batch_size,
                        )

                        self.conn.execute("BEGIN")
                        for part in _chunks(snapshot_params, batch_size):
                            cur.executemany(snapshot_insert_sql, part)
                            saved_snapshot += len(part)
                        for part in _chunks(raw_params, batch_size):
                            cur.executemany(raw_insert_sql, part)
                            saved_raw += len(part)

                        # Finalize all statements before commit. This prevents:
                        #   cannot commit transaction - SQL statements in progress
                        try:
                            cur.close()
                        finally:
                            cur = None

                        self.conn.commit()
                        committed = True

                        self.total_flushed_snapshot += saved_snapshot
                        self.total_flushed_raw += saved_raw
                        self.last_flush_at = dt.datetime.now().isoformat(timespec="seconds")
                        self.last_flush_elapsed_sec = time.time() - t0
                        self.last_error = None
                        try:
                            self._mark_runtime()
                        except Exception:
                            pass
                        logger.info(
                            "[RANKING DB WRITER] flush done v4 snapshot=%d raw=%d legacy=0 elapsed=%.3fs buffer_after=%d",
                            saved_snapshot, saved_raw, self.last_flush_elapsed_sec, _buffer_count(self),
                        )
                        return True

                except sqlite3.OperationalError as e:
                    last_error = str(e)
                    try:
                        if getattr(self, "conn", None) is not None and not committed:
                            self.conn.rollback()
                    except Exception:
                        pass

                    retryable = _is_locked_error(e) or _is_statement_progress_error(e)
                    if not retryable:
                        _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                        _mark_flush_error(self, last_error, t0)
                        logger.exception("[RANKING DB WRITER] flush failed v4 non-retryable")
                        return False

                    if attempt >= int(retry_max):
                        _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                        _mark_flush_error(self, last_error, t0)
                        logger.warning(
                            "[RANKING DB WRITER LOCK PATCH] retryable persists v4 attempt=%s/%s returned_to_buffer=%s err=%s",
                            attempt, retry_max, _buffer_count(self), e,
                        )
                        return False

                    sleep_sec = min(float(retry_max_sleep_sec), float(retry_base_sec) * (2 ** attempt))
                    logger.warning(
                        "[RANKING DB WRITER LOCK PATCH] retry v4 attempt=%s/%s sleep=%.2fs rows snapshot=%d raw=%d buffer=%s err=%s",
                        attempt + 1, retry_max, sleep_sec, len(snapshot_rows), len(raw_rows), _buffer_count(self), e,
                    )
                    time.sleep(sleep_sec)

                except Exception as e:
                    last_error = str(e)
                    try:
                        if getattr(self, "conn", None) is not None and not committed:
                            self.conn.rollback()
                    except Exception:
                        pass
                    _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                    _mark_flush_error(self, last_error, t0)
                    logger.exception("[RANKING DB WRITER] flush failed v4 returned rows to buffer")
                    return False

                finally:
                    try:
                        if cur is not None:
                            cur.close()
                    except Exception:
                        pass

            _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
            _mark_flush_error(self, last_error or "unknown flush failure", t0)
            return False

        finally:
            try:
                with self.lock:
                    self._flush_v4_running = False
                    self._flush_v3_running = False
            except Exception:
                try:
                    self._flush_v4_running = False
                    self._flush_v3_running = False
                except Exception:
                    pass

    cls.flush = flush_replace_upsert
    cls._lock_retry_patch_v4_installed = True
    cls._lock_retry_patch_v3_installed = True
    cls._lock_retry_patch_v2_installed = True
    cls._lock_retry_patch_installed = True

    logger.warning(
        "[RANKING DB WRITER LOCK PATCH] installed v4 finalize-before-commit busy_timeout_ms=%s retry_max=%s base=%.2fs max_sleep=%.2fs batch_size=%s",
        busy_timeout_ms, retry_max, retry_base_sec, retry_max_sleep_sec, batch_size,
    )
    return True


try:
    install_ranking_db_writer_lock_patch()
except Exception:
    logger.exception("[RANKING DB WRITER LOCK PATCH] auto install failed")


__all__ = ["install_ranking_db_writer_lock_patch"]
