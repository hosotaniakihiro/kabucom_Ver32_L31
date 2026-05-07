# ============================================================
# File   : trading/summary/persistence/core/upsert_executor.py
# Version: PRODUCTION-STABLE-UPSERT-EXECUTOR-V9-VERIFY-CHECKPOINT
# ------------------------------------------------------------
# Purpose:
#   summary 系テーブルへの bulk upsert 公開API。
#
# Common modules:
#   - database/sqlite/retry.py
#   - database/sqlite/normalize.py
#   - database/sqlite/inspector.py
#   - database/sqlite/sql_builder.py
#
# Summary modules:
#   - write_gate.py
#   - chunk_utils.py
#   - table_filter.py
#   - sql_builder.py
#   - delete_insert_fallback.py
#
# REV8:
#   - stock_summary_* への保存時、DataFrame 由来の id を必ず除外する
#   - id は SQLite 側の自動採番主キーとして扱う
#   - ON CONFLICT(symbol, datetime) UPSERT 中の UNIQUE(id) 衝突を防ぐ
#
# REV9:
#   - UPSERT成功後に count/max(datetime) を読み戻してログ出力
#   - SQLite WAL に残ってDBビューアで見えにくいケースに備え、
#     PRAGMA wal_checkpoint(PASSIVE) を安全実行
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from sqlalchemy.engine import Engine

from database.sqlite.inspector import (
    invalidate_constraint_cache,
    invalidate_table_columns_cache,
    table_has_unique_constraint,
)
from database.sqlite.normalize import normalize_rows_for_sqlite
from database.sqlite.retry import DEFAULT_BUSY_TIMEOUT_MS, run_sql_many_with_retry

from .chunk_utils import chunked, normalize_rows
from .delete_insert_fallback import delete_then_insert_chunk
from .sql_builder import build_summary_upsert_sql
from .table_filter import filter_rows_to_existing_columns
from .write_gate import summary_write_gate

logger = logging.getLogger(__name__)

try:
    from database.session import get_summary_engine  # type: ignore
except Exception:
    get_summary_engine = None  # type: ignore

_DEFAULT_CHUNK_SIZE = int(os.environ.get("SUMMARY_UPSERT_CHUNK_SIZE", "75"))
_DEFAULT_RETRY = int(os.environ.get("SUMMARY_UPSERT_RETRY", "12"))
_DEFAULT_SLEEP_BASE = float(os.environ.get("SUMMARY_UPSERT_SLEEP_BASE", "0.45"))
_CHECKPOINT_AFTER_UPSERT = os.environ.get("SUMMARY_UPSERT_WAL_CHECKPOINT", "1").strip().lower() not in {"0", "false", "no", "off"}


def _table_name_from_interval(interval: int) -> str:
    mapping = {
        1: "stock_summary_1min",
        3: "stock_summary_3min",
        5: "stock_summary_5min",
        10: "stock_summary_10min",
        15: "stock_summary_15min",
        30: "stock_summary_30min",
        60: "stock_summary_60min",
    }
    return mapping.get(int(interval), f"stock_summary_{int(interval)}min")


def _get_engine(engine: Optional[Engine] = None) -> Engine:
    if engine is not None:
        return engine
    if callable(get_summary_engine):
        return get_summary_engine()
    raise RuntimeError("summary engine is not available")


def _drop_summary_autoincrement_id(rows: List[dict], *, table_name: str, interval: int) -> List[dict]:
    """
    stock_summary_* の id は SQLite 側の自動採番主キーとして扱う。

    DataFrame に古い id が混入したまま INSERT/UPSERT すると、
    ON CONFLICT(symbol, datetime) の対象外である UNIQUE(id) に衝突し、
    次のようなエラーになる。

      sqlite3.IntegrityError: UNIQUE constraint failed: stock_summary_1min.id

    そのため summary 保存では id を SQL に渡さない。
    """
    if not rows:
        return rows

    table = str(table_name or "")
    if not table.startswith("stock_summary_"):
        return rows

    has_id = any(isinstance(r, dict) and "id" in r for r in rows)
    if not has_id:
        return rows

    out: List[dict] = []
    dropped = 0

    for r in rows:
        if not isinstance(r, dict):
            out.append(r)
            continue

        if "id" in r:
            nr = dict(r)
            nr.pop("id", None)
            out.append(nr)
            dropped += 1
        else:
            out.append(r)

    logger.warning(
        "[UPSERT] dropped autoincrement id before summary save interval=%s table=%s rows=%s dropped=%s",
        interval,
        table_name,
        len(rows),
        dropped,
    )
    return out


def _checkpoint_and_verify_summary_db(engine: Engine, table_name: str, interval: int, saved_rows: int) -> None:
    """
    UPSERT後の見える化。

    - count / max(datetime) を同じ engine で読み戻してログに出す。
    - WALモードでは書き込み直後の変更が -wal 側に残るため、
      DBビューアや別プロセスで見えにくい場合がある。
      そのため PASSIVE checkpoint を軽く実行する。

    失敗しても保存結果は成功扱いのままにする。
    """
    if not str(table_name or "").startswith("stock_summary_"):
        return

    try:
        with engine.connect() as conn:
            try:
                row = conn.exec_driver_sql(
                    f'SELECT COUNT(*) AS cnt, MAX(datetime) AS max_dt FROM "{table_name}"'
                ).fetchone()
                cnt = row[0] if row is not None and len(row) > 0 else None
                max_dt = row[1] if row is not None and len(row) > 1 else None
                logger.warning(
                    "[UPSERT VERIFY] summary table=%s interval=%s saved_rows=%s db_count=%s db_max_datetime=%s",
                    table_name,
                    interval,
                    saved_rows,
                    cnt,
                    max_dt,
                )
            except Exception:
                logger.debug(
                    "[UPSERT VERIFY] readback failed interval=%s table=%s",
                    interval,
                    table_name,
                    exc_info=True,
                )

            if _CHECKPOINT_AFTER_UPSERT:
                try:
                    ck = conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                    logger.warning(
                        "[UPSERT CHECKPOINT] summary table=%s interval=%s result=%s",
                        table_name,
                        interval,
                        tuple(ck) if ck is not None else None,
                    )
                except Exception:
                    logger.debug(
                        "[UPSERT CHECKPOINT] skipped/failed interval=%s table=%s",
                        interval,
                        table_name,
                        exc_info=True,
                    )

    except Exception:
        logger.debug(
            "[UPSERT VERIFY] connect failed interval=%s table=%s",
            interval,
            table_name,
            exc_info=True,
        )


def execute_chunk_with_retry(
    *,
    engine: Engine,
    sql: str,
    chunk: List[dict],
    interval: int,
    table_name: str,
    chunk_no: int,
    total_chunks: int,
    retry: int = _DEFAULT_RETRY,
    sleep_base: float = _DEFAULT_SLEEP_BASE,
) -> None:
    """
    既存互換API。
    1 chunk の UPSERT を lock retry 付きで実行する。
    """
    safe_chunk = normalize_rows_for_sqlite(chunk)
    safe_chunk = filter_rows_to_existing_columns(engine, table_name, safe_chunk)
    safe_chunk = _drop_summary_autoincrement_id(
        safe_chunk,
        table_name=table_name,
        interval=int(interval),
    )

    if not safe_chunk:
        logger.warning(
            "[UPSERT] sql_chunk skipped after table-column filter interval=%s table=%s chunk=%s/%s",
            interval,
            table_name,
            chunk_no,
            total_chunks,
        )
        return

    run_sql_many_with_retry(
        engine=engine,
        sql=sql,
        params=safe_chunk,
        log_prefix="[UPSERT] sql_chunk",
        interval=int(interval),
        table_name=table_name,
        chunk_no=chunk_no,
        total_chunks=total_chunks,
        retry=max(1, int(retry or _DEFAULT_RETRY)),
        sleep_base=max(0.05, float(sleep_base or _DEFAULT_SLEEP_BASE)),
    )


def execute_upsert(
    rows: Any,
    interval: int,
    engine: Optional[Engine] = None,
    table_name: Optional[str] = None,
    *,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    retry: int = _DEFAULT_RETRY,
    sleep_base: float = _DEFAULT_SLEEP_BASE,
    skip_if_busy: bool = False,
    write_gate_timeout: Optional[float] = None,
) -> int:
    """
    summary テーブルへ bulk upsert する。

    既存呼び出し互換:
      execute_upsert(rows, interval, engine=None, table_name=None)
    """
    work = normalize_rows(rows)
    if not work:
        logger.info("[UPSERT] no rows interval=%s", interval)
        return 0

    eng = _get_engine(engine)
    table = table_name or _table_name_from_interval(int(interval))

    # SQL生成前に id を除外する。
    # ここで除外しないと INSERT列 / UPDATE SET の両方に id が含まれ、
    # UNIQUE(id) と ON CONFLICT(symbol, datetime) が噛み合わず失敗する。
    work = _drop_summary_autoincrement_id(
        work,
        table_name=table,
        interval=int(interval),
    )

    chunk_size = max(1, int(chunk_size or _DEFAULT_CHUNK_SIZE))
    retry = max(1, int(retry or _DEFAULT_RETRY))
    sleep_base = max(0.05, float(sleep_base or _DEFAULT_SLEEP_BASE))

    with summary_write_gate(
        table_name=table,
        interval=interval,
        reason="execute_upsert",
        timeout=write_gate_timeout,
        skip_if_busy=skip_if_busy,
    ) as acquired:
        if not acquired:
            logger.warning(
                "[UPSERT] skipped by write_gate interval=%s table=%s rows=%s",
                interval,
                table,
                len(work),
            )
            return 0

        work = filter_rows_to_existing_columns(eng, table, work)
        work = _drop_summary_autoincrement_id(
            work,
            table_name=table,
            interval=int(interval),
        )

        if not work:
            logger.warning(
                "[UPSERT] no rows after table-column filter interval=%s table=%s",
                interval,
                table,
            )
            return 0

        logger.info(
            "[UPSERT] phase=records interval=%s table=%s rows=%s chunk_size=%s retry=%s "
            "sleep_base=%.2f busy_timeout_ms=%s elapsed=0.000s",
            interval,
            table,
            len(work),
            chunk_size,
            retry,
            sleep_base,
            DEFAULT_BUSY_TIMEOUT_MS,
        )

        has_unique = table_has_unique_constraint(eng, table, ("symbol", "datetime"))

        if has_unique:
            sql = build_summary_upsert_sql(table, work)

            for chunk_no, total_chunks, chunk in chunked(work, chunk_size):
                try:
                    execute_chunk_with_retry(
                        engine=eng,
                        sql=sql,
                        chunk=chunk,
                        interval=int(interval),
                        table_name=table,
                        chunk_no=chunk_no,
                        total_chunks=total_chunks,
                        retry=retry,
                        sleep_base=sleep_base,
                    )

                except Exception as e:
                    msg = str(e)

                    if "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint" in msg:
                        logger.warning(
                            "[UPSERT] unique constraint cache stale -> switch fallback interval=%s table=%s",
                            interval,
                            table,
                        )
                        invalidate_constraint_cache(table)
                        has_unique = False
                        break

                    raise

            if has_unique:
                saved = len(work)
                _checkpoint_and_verify_summary_db(eng, table, int(interval), saved)
                return saved

        logger.warning(
            "[UPSERT] using delete+insert fallback interval=%s table=%s rows=%s chunk_size=%s retry=%s",
            interval,
            table,
            len(work),
            chunk_size,
            retry,
        )

        for chunk_no, total_chunks, chunk in chunked(work, chunk_size):
            safe_chunk = _drop_summary_autoincrement_id(
                list(chunk),
                table_name=table,
                interval=int(interval),
            )
            delete_then_insert_chunk(
                engine=eng,
                table_name=table,
                chunk=safe_chunk,
                interval=int(interval),
                chunk_no=chunk_no,
                total_chunks=total_chunks,
                retry=retry,
                sleep_base=sleep_base,
            )

        saved = len(work)
        _checkpoint_and_verify_summary_db(eng, table, int(interval), saved)
        return saved


def execute_chunk_with_retry_compat(
    conn_or_engine: Any,
    sql: str,
    chunk: List[dict],
    interval: int,
    table_name: str,
    chunk_no: int = 1,
    total_chunks: int = 1,
    retry: int = _DEFAULT_RETRY,
    sleep_base: float = _DEFAULT_SLEEP_BASE,
) -> None:
    """
    旧呼び出し互換。
    conn が渡っても engine が渡っても受ける。
    """
    engine: Optional[Engine] = None

    try:
        if hasattr(conn_or_engine, "engine"):
            engine = conn_or_engine.engine
        elif hasattr(conn_or_engine, "begin") and hasattr(conn_or_engine, "connect"):
            engine = conn_or_engine
    except Exception:
        engine = None

    if engine is None:
        engine = _get_engine(None)

    execute_chunk_with_retry(
        engine=engine,
        sql=sql,
        chunk=chunk,
        interval=interval,
        table_name=table_name,
        chunk_no=chunk_no,
        total_chunks=total_chunks,
        retry=retry,
        sleep_base=sleep_base,
    )


__all__ = [
    "execute_upsert",
    "execute_chunk_with_retry",
    "execute_chunk_with_retry_compat",
    "invalidate_constraint_cache",
    "invalidate_table_columns_cache",
]
