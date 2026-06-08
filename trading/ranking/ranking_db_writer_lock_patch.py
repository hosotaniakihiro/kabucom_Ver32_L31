# ============================================================
# File   : trading/ranking/ranking_db_writer_lock_patch.py
# Version: PRODUCTION-STABLE-RANKING-DB-WRITER-LOCK-PATCH-V5-DEDICATED-CONNECTION
# ------------------------------------------------------------
# Purpose:
#   ranking_db_writer.py 本体を大きく壊さず、SQLite locked / cursor再入 /
#   commit時未完了SQL 対策を後付けする。
#
# V5:
#   - flush 時は writer.conn / writer.cursor を使わず、専用の短命sqlite接続で保存する。
#   - self.cursor に残った PRAGMA / SELECT statement の影響を commit へ持ち込まない。
#   - locked / statements in progress 時は rollback + close してから retry。
#   - retry sleep を短縮し、ranking writer が entry controller を長時間詰まらせにくくする。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
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
    size = max(1, int(size or 200))
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
    cur.execute(sql)
    try:
        cur.fetchall()
    except Exception:
        pass


def _resolve_db_path(writer: Any) -> Path:
    try:
        dbp = getattr(writer, "db_path", None)
        if dbp:
            return Path(dbp)
    except Exception:
        pass
    try:
        from database.paths.ranking_paths import get_ranking_db_path
        return Path(get_ranking_db_path())
    except Exception:
        pass
    try:
        from trading.ranking.ranking_db_writer import _resolve_ranking_db_path
        return Path(_resolve_ranking_db_path())
    except Exception:
        raise


def _open_dedicated_connection(db_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=max(1.0, busy_timeout_ms / 1000.0), check_same_thread=False)
    cur = conn.cursor()
    try:
        _exec_and_drain(cur, f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        try:
            _exec_and_drain(cur, "PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            logger.debug("[RANKING DB WRITER LOCK PATCH] journal_mode pragma skipped", exc_info=True)
        _exec_and_drain(cur, "PRAGMA synchronous=NORMAL;")
        _exec_and_drain(cur, "PRAGMA temp_store=MEMORY;")
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return conn


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

    if getattr(cls, "_lock_retry_patch_v5_installed", False):
        return True

    busy_timeout_ms = _env_int("RANKING_WRITER_BUSY_TIMEOUT_MS", 8000)
    retry_max = _env_int("RANKING_WRITER_LOCK_RETRY_MAX", 3)
    retry_base_sec = _env_float("RANKING_WRITER_LOCK_RETRY_BASE_SEC", 0.25)
    retry_max_sleep_sec = _env_float("RANKING_WRITER_LOCK_RETRY_MAX_SLEEP_SEC", 1.0)
    batch_size = _env_int("RANKING_WRITER_SQL_BATCH_SIZE", 150)

    try:
        target.DEFAULT_BUSY_TIMEOUT_MS = int(busy_timeout_ms)
    except Exception:
        pass

    orig_flush = cls.flush

    def flush_replace_upsert(self, *args, **kwargs) -> bool:
        with self.lock:
            if getattr(self, "_flush_v5_running", False):
                logger.warning("[RANKING DB WRITER LOCK PATCH] flush skipped reason=already_running buffer=%s", _buffer_count(self))
                return False
            if not (self.raw_buffer or self.snapshot_buffer or self.legacy_buffer):
                logger.debug("[RANKING DB WRITER] flush skipped empty")
                return True
            self._flush_v5_running = True
            self._flush_v4_running = True
            raw_rows = list(self.raw_buffer or [])
            snapshot_rows = list(self.snapshot_buffer or [])
            legacy_rows = list(self.legacy_buffer or [])
            self.raw_buffer = []
            self.snapshot_buffer = []
            self.legacy_buffer = []

        try:
            # legacy rows は既存実装に任せる。旧カテゴリ別テーブル作成/保存の互換性を維持する。
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

            db_path = _resolve_db_path(self)
            last_snapshot = 0
            last_raw = 0
            for attempt in range(int(retry_max) + 1):
                conn: sqlite3.Connection | None = None
                cur: sqlite3.Cursor | None = None
                committed = False
                try:
                    conn = _open_dedicated_connection(db_path, busy_timeout_ms)
                    cur = conn.cursor()
                    logger.info(
                        "[RANKING DB WRITER] flush prepare v5 dedicated-conn raw=%d snapshot=%d legacy=%d snapshot_types=%s raw_types=%s batch_size=%d db=%s",
                        len(raw_rows), len(snapshot_rows), len(legacy_rows),
                        _type_counts(target, snapshot_rows), _type_counts(target, raw_rows), batch_size, db_path,
                    )
                    conn.execute("BEGIN IMMEDIATE")
                    saved_snapshot = 0
                    saved_raw = 0
                    for part in _chunks(snapshot_params, batch_size):
                        cur.executemany(snapshot_insert_sql, part)
                        saved_snapshot += len(part)
                    for part in _chunks(raw_params, batch_size):
                        cur.executemany(raw_insert_sql, part)
                        saved_raw += len(part)
                    try:
                        cur.close()
                    finally:
                        cur = None
                    conn.commit()
                    committed = True
                    last_snapshot = saved_snapshot
                    last_raw = saved_raw
                    with self.lock:
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
                        "[RANKING DB WRITER] flush done v5 snapshot=%d raw=%d legacy=0 elapsed=%.3fs buffer_after=%d",
                        saved_snapshot, saved_raw, time.time() - t0, _buffer_count(self),
                    )
                    return True

                except sqlite3.OperationalError as e:
                    last_error = str(e)
                    try:
                        if conn is not None and not committed:
                            conn.rollback()
                    except Exception:
                        pass
                    retryable = _is_locked_error(e) or _is_statement_progress_error(e)
                    if not retryable:
                        _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                        _mark_flush_error(self, last_error, t0)
                        logger.exception("[RANKING DB WRITER] flush failed v5 non-retryable")
                        return False
                    if attempt >= int(retry_max):
                        _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                        _mark_flush_error(self, last_error, t0)
                        logger.warning(
                            "[RANKING DB WRITER LOCK PATCH] retryable persists v5 attempt=%s/%s returned_to_buffer=%s saved_snapshot=%s saved_raw=%s err=%s",
                            attempt, retry_max, _buffer_count(self), last_snapshot, last_raw, e,
                        )
                        return False
                    sleep_sec = min(float(retry_max_sleep_sec), float(retry_base_sec) * (2 ** attempt))
                    logger.warning(
                        "[RANKING DB WRITER LOCK PATCH] retry v5 attempt=%s/%s sleep=%.2fs rows snapshot=%d raw=%d buffer=%s err=%s",
                        attempt + 1, retry_max, sleep_sec, len(snapshot_rows), len(raw_rows), _buffer_count(self), e,
                    )
                    time.sleep(sleep_sec)

                except Exception as e:
                    last_error = str(e)
                    try:
                        if conn is not None and not committed:
                            conn.rollback()
                    except Exception:
                        pass
                    _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                    _mark_flush_error(self, last_error, t0)
                    logger.exception("[RANKING DB WRITER] flush failed v5 returned rows to buffer")
                    return False
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

            _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
            _mark_flush_error(self, last_error or "unknown flush failure", t0)
            return False

        finally:
            try:
                with self.lock:
                    self._flush_v5_running = False
                    self._flush_v4_running = False
                    self._flush_v3_running = False
            except Exception:
                try:
                    self._flush_v5_running = False
                    self._flush_v4_running = False
                    self._flush_v3_running = False
                except Exception:
                    pass

    cls.flush = flush_replace_upsert
    cls._lock_retry_patch_v5_installed = True
    cls._lock_retry_patch_v4_installed = True
    cls._lock_retry_patch_v3_installed = True
    cls._lock_retry_patch_v2_installed = True
    cls._lock_retry_patch_installed = True

    logger.warning(
        "[RANKING DB WRITER LOCK PATCH] installed v5 dedicated-connection busy_timeout_ms=%s retry_max=%s base=%.2fs max_sleep=%.2fs batch_size=%s",
        busy_timeout_ms, retry_max, retry_base_sec, retry_max_sleep_sec, batch_size,
    )
    return True


def install() -> bool:
    return install_ranking_db_writer_lock_patch()


try:
    install()
except Exception:
    logger.exception("[RANKING DB WRITER LOCK PATCH] auto install failed")


__all__ = ["install", "install_ranking_db_writer_lock_patch"]
