# ============================================================
# File   : trading/ranking/ranking_db_writer_lock_patch.py
# Version: PRODUCTION-STABLE-RANKING-DB-WRITER-LOCK-PATCH-V2-REPLACE-UPsert
# ------------------------------------------------------------
# Purpose:
#   ranking_db_writer.py 本体を大きく壊さず、SQLite locked 対策を後付けする。
#
# Why:
#   ranking DB は ranking_db_writer / ranking_summary / schema ensure / AI reader が
#   同じ rankingYYYYMMDD.db を触るため、瞬間的に database is locked が起きる。
#
# V1 problem:
#   元 flush は snapshot 保存時に
#       DELETE FROM ranking_snapshot_1min ... executemany
#       INSERT INTO ranking_snapshot_1min ... executemany
#   を行う。
#   DELETE がロックを取りやすく、リトライしても同じ DELETE で再度詰まる。
#
# V2 fix:
#   - RankingDBWriter.flush() を置換
#   - legacy_buffer が無い通常運用では DELETE を使わず INSERT OR REPLACE
#   - transaction を BEGIN IMMEDIATE ではなく通常BEGINにしてロックを短時間化
#   - batch size を分割し、巨大executemanyで長時間ロックしない
#   - database locked 時は rollback → buffer先頭へ戻す → backoff retry
#   - legacy保存がある場合だけ元flushへfallback
#   - DEFAULT_BUSY_TIMEOUT_MS を既定 30000ms へ延長
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# env helpers
# ============================================================

def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(v)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_locked_error(err: Any) -> bool:
    s = str(err or "").lower()
    return "database is locked" in s or "database table is locked" in s or "locked" in s


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
    """
    flush失敗時、今回取り出した行を先頭へ戻す。
    後続で追加されたbufferを消さない。
    """
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


# ============================================================
# installer
# ============================================================

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

    if getattr(cls, "_lock_retry_patch_v2_installed", False):
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
        """
        snapshot/raw の通常保存を短時間 transaction で保存する。

        注意:
          - legacy_buffer がある場合は互換性優先で元flushへfallback。
          - snapshot は ranking_snapshot_schema の PRIMARY KEY
            (symbol, datetime, ranking_type, market) に対して INSERT OR REPLACE。
        """
        with self.lock:
            if not (self.raw_buffer or self.snapshot_buffer or self.legacy_buffer):
                logger.debug("[RANKING DB WRITER] flush skipped empty")
                return True

            raw_rows = list(self.raw_buffer or [])
            snapshot_rows = list(self.snapshot_buffer or [])
            legacy_rows = list(self.legacy_buffer or [])

            self.raw_buffer = []
            self.snapshot_buffer = []
            self.legacy_buffer = []

        # legacy保存は既存テーブル個別処理があるため元実装へ戻す。
        # ただし取り出したbufferを戻してから呼ぶ。
        if legacy_rows:
            _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
            return bool(orig_flush(self, *args, **kwargs))

        t0 = time.time()
        last_error = ""

        for attempt in range(int(retry_max) + 1):
            saved_snapshot = 0
            saved_raw = 0

            try:
                with self.lock:
                    self._ensure_connection()

                    assert self.conn is not None
                    assert self.cursor is not None

                    self.cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
                    self.cursor.execute("PRAGMA journal_mode=WAL;")
                    self.cursor.execute("PRAGMA synchronous=NORMAL;")
                    self.cursor.execute("PRAGMA temp_store=MEMORY;")

                    logger.info(
                        "[RANKING DB WRITER] flush prepare v2 raw=%d snapshot=%d legacy=%d snapshot_types=%s raw_types=%s batch_size=%d",
                        len(raw_rows),
                        len(snapshot_rows),
                        len(legacy_rows),
                        _type_counts(target, snapshot_rows),
                        _type_counts(target, raw_rows),
                        batch_size,
                    )

                    snapshot_insert_sql = f"""
                    INSERT OR REPLACE INTO {target.quote_ident(target.SNAPSHOT_TABLE)} (
                        symbol,
                        datetime,
                        snapshot_time,
                        symbolname,
                        current_price,
                        price,
                        change_percentage,
                        change_rate,
                        trading_volume,
                        volume,
                        trading_value,
                        turnover,
                        tick_count,
                        ranking_type,
                        rank_type,
                        category,
                        market,
                        exchange,
                        source,
                        rank,
                        created_at,
                        inserted_at
                    )
                    VALUES ({",".join(["?"] * 22)})
                    """

                    raw_insert_sql = f"""
                    INSERT OR IGNORE INTO {target.quote_ident(target.RAW_TABLE)} (
                        ingest_id,
                        symbol,
                        datetime,
                        snapshot_time,
                        symbolname,
                        current_price,
                        price,
                        change_percentage,
                        change_rate,
                        change_ratio,
                        trading_volume,
                        volume,
                        trading_value,
                        turnover,
                        tick_count,
                        ranking_type,
                        rank_type,
                        category,
                        market,
                        exchange,
                        source,
                        rank,
                        date,
                        time,
                        raw_json,
                        received_at,
                        created_at,
                        inserted_at,
                        updated_at
                    )
                    VALUES ({",".join(["?"] * 29)})
                    """

                    snapshot_params: list[tuple] = []
                    for row in snapshot_rows:
                        try:
                            snapshot_params.append(target.normalize_snapshot_row(row))
                        except Exception:
                            logger.warning(
                                "[RANKING DB WRITER] snapshot normalize skipped row=%r",
                                row,
                                exc_info=True,
                            )

                    raw_params: list[tuple] = []
                    for row in raw_rows:
                        try:
                            raw_params.append(target.normalize_raw_row(row))
                        except Exception:
                            logger.warning(
                                "[RANKING DB WRITER] raw normalize skipped row=%r",
                                row,
                                exc_info=True,
                            )

                    self.conn.execute("BEGIN")

                    for part in _chunks(snapshot_params, batch_size):
                        self.cursor.executemany(snapshot_insert_sql, part)
                        saved_snapshot += len(part)

                    for part in _chunks(raw_params, batch_size):
                        self.cursor.executemany(raw_insert_sql, part)
                        saved_raw += len(part)

                    self.conn.commit()

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
                        "[RANKING DB WRITER] flush done v2 snapshot=%d raw=%d legacy=0 elapsed=%.3fs buffer_after=%d",
                        saved_snapshot,
                        saved_raw,
                        self.last_flush_elapsed_sec,
                        _buffer_count(self),
                    )
                    return True

            except sqlite3.OperationalError as e:
                last_error = str(e)
                try:
                    if getattr(self, "conn", None) is not None:
                        self.conn.rollback()
                except Exception:
                    pass

                if not _is_locked_error(e):
                    _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                    try:
                        self.total_flush_errors += 1
                        self.last_error = last_error
                        self.last_flush_elapsed_sec = time.time() - t0
                        self._mark_runtime()
                    except Exception:
                        pass
                    logger.exception("[RANKING DB WRITER] flush failed v2 non-lock")
                    return False

                if attempt >= int(retry_max):
                    _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                    try:
                        self.total_flush_errors += 1
                        self.last_error = last_error
                        self.last_flush_elapsed_sec = time.time() - t0
                        self._mark_runtime()
                    except Exception:
                        pass
                    logger.warning(
                        "[RANKING DB WRITER LOCK PATCH] locked persists v2 attempt=%s/%s returned_to_buffer=%s err=%s",
                        attempt,
                        retry_max,
                        _buffer_count(self),
                        e,
                    )
                    return False

                sleep_sec = min(float(retry_max_sleep_sec), float(retry_base_sec) * (2 ** attempt))
                logger.warning(
                    "[RANKING DB WRITER LOCK PATCH] database locked retry v2 attempt=%s/%s sleep=%.2fs rows snapshot=%d raw=%d buffer=%s err=%s",
                    attempt + 1,
                    retry_max,
                    sleep_sec,
                    len(snapshot_rows),
                    len(raw_rows),
                    _buffer_count(self),
                    e,
                )
                time.sleep(sleep_sec)

            except Exception as e:
                last_error = str(e)
                try:
                    if getattr(self, "conn", None) is not None:
                        self.conn.rollback()
                except Exception:
                    pass

                _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
                try:
                    self.total_flush_errors += 1
                    self.last_error = last_error
                    self.last_flush_elapsed_sec = time.time() - t0
                    self._mark_runtime()
                except Exception:
                    pass

                logger.exception("[RANKING DB WRITER] flush failed v2 returned rows to buffer")
                return False

        _return_to_front(self, raw_rows, snapshot_rows, legacy_rows)
        try:
            self.total_flush_errors += 1
            self.last_error = last_error or "unknown flush failure"
            self.last_flush_elapsed_sec = time.time() - t0
            self._mark_runtime()
        except Exception:
            pass
        return False

    cls.flush = flush_replace_upsert
    cls._lock_retry_patch_v2_installed = True
    cls._lock_retry_patch_installed = True

    logger.warning(
        "[RANKING DB WRITER LOCK PATCH] installed v2 replace-upsert busy_timeout_ms=%s retry_max=%s base=%.2fs max_sleep=%.2fs batch_size=%s",
        busy_timeout_ms,
        retry_max,
        retry_base_sec,
        retry_max_sleep_sec,
        batch_size,
    )
    return True


try:
    install_ranking_db_writer_lock_patch()
except Exception:
    logger.exception("[RANKING DB WRITER LOCK PATCH] auto install failed")


__all__ = ["install_ranking_db_writer_lock_patch"]
