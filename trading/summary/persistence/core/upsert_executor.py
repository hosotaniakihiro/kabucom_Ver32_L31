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
import sys
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


# ============================================================
# summary write gate runtime tuning
# (旧 core/startup/summary_write_gate_runtime_patch.py REV3 から移設)
# ------------------------------------------------------------
# write gate が無制限待ちになり 1m/5m/recovery が互いに詰まる問題への対策。
# main.py / entry-only では起動時・復旧時の巨大UPSERTを同期実行せず
# リアルタイム発注処理を守る。main_database.py / yahoo_complement /
# summary_database_runner 等の保存担当プロセスでは巨大UPSERTを捨てない。
#
# 旧パッチは core/startup/__init__.py 経由でのみ install されていたため、
# DB専用子プロセスでは一切適用されなかった。本文化後も
# _write_gate_runtime_enabled() で同じスコープを保つ。
# ============================================================

_WRITE_GATE_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)

_WRITE_GATE_SAVE_OWNER_MARKERS = (
    "main_database.py",
    "data_collectors_runner.py",
    "summary_database_runner.py",
    "yahoo_complement",
    "yahoo_complement_runner",
    "run_night_yahoo",
    "run_update_daily_data_from_yahoo",
    "run_night_yahoo_full_summary",
    "summary_recovery",
)

_WRITE_GATE_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_WRITE_GATE_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _gate_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in _WRITE_GATE_TRUE:
            return True
        if s in _WRITE_GATE_FALSE:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _gate_env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _gate_env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _gate_argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        return ""


def _write_gate_runtime_enabled() -> bool:
    """このプロセスで write gate の runtime tuning を適用するか。

    SUMMARY_WRITE_GATE_RUNTIME_ENABLED=1/0 で明示的に上書きできる。
    CORE_STARTUP_FORCE_FULL_INIT_PATCHES=1 のときは DB プロセスでも適用する。
    """
    try:
        override = os.environ.get("SUMMARY_WRITE_GATE_RUNTIME_ENABLED")
        if override is not None and str(override).strip() != "":
            return _gate_env_bool("SUMMARY_WRITE_GATE_RUNTIME_ENABLED", True)
        if _gate_env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        return not any(name in _gate_argv_text() for name in _WRITE_GATE_DB_PROCESS_NAMES)
    except Exception:
        return True


def _is_summary_save_owner_process() -> bool:
    """summary DB保存を担うプロセスでは巨大UPSERTを捨てない。"""
    if any(x in _gate_argv_text() for x in _WRITE_GATE_SAVE_OWNER_MARKERS):
        return True
    return any(
        _gate_env_bool(x, False)
        for x in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_YAHOO_COMPLEMENT_PROCESS",
            "SUMMARY_DB_SAVE_OWNER_PROCESS",
        )
    )


def _is_entry_only_process() -> bool:
    if _gate_env_bool("SUMMARY_MAIN_ENTRY_ONLY", False):
        return True
    if "main.py" in _gate_argv_text() and not _is_summary_save_owner_process():
        return True
    return False


def _gate_interval_int(v: Any, default: int = 1) -> int:
    try:
        return int(float(str(v).replace("min", "").strip()))
    except Exception:
        return int(default)


def _gate_row_count(rows: Any) -> int:
    try:
        return int(len(rows))
    except Exception:
        pass
    try:
        if hasattr(rows, "shape"):
            return int(rows.shape[0])
    except Exception:
        pass
    try:
        return int(sum(1 for _ in rows))
    except Exception:
        return -1


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
    # ----------------------------------------------------
    # write gate runtime tuning
    # (旧 core/startup/summary_write_gate_runtime_patch.py REV3 から移設)
    # 行数判定は normalize_rows の前の生の行数で行う。
    # ----------------------------------------------------
    if _write_gate_runtime_enabled():
        iv = _gate_interval_int(interval, 1)
        row_count = _gate_row_count(rows)
        table_for_log = str(table_name) if table_name else f"stock_summary_{iv}min"
        save_owner = _is_summary_save_owner_process()
        entry_only = _is_entry_only_process()

        # 巨大 recovery / closed-market rebuild 系 UPSERT の同期実行制御。
        # main.py はリアルタイム発注を守るため skip 可。DB保存担当は skip しない。
        huge_skip_enabled = _gate_env_bool(
            "SUMMARY_SKIP_HUGE_UPSERT", bool(entry_only and not save_owner)
        )
        huge_threshold = _gate_env_int("SUMMARY_HUGE_UPSERT_SKIP_ROWS", 50000)
        if save_owner and _gate_env_bool("SUMMARY_SAVE_OWNER_DISABLE_HUGE_SKIP", True):
            huge_skip_enabled = False

        if huge_skip_enabled and row_count >= huge_threshold:
            logger.error(
                "[SUMMARY WRITE GATE] huge upsert skipped to avoid sqlite lock "
                "interval=%s table=%s rows=%s threshold=%s save_owner=%s entry_only=%s "
                "hint=set SUMMARY_SKIP_HUGE_UPSERT=0 to force, or run recovery/night batch separately",
                iv, table_for_log, row_count, huge_threshold, save_owner, entry_only,
            )
            return 0

        # 中規模以上はロックを取りに行く時間を短くする。
        # 保存担当プロセスでは skip_if_busy を既定 False にして補完保存を落とさない。
        medium_threshold = _gate_env_int("SUMMARY_MEDIUM_UPSERT_FAST_SKIP_ROWS", 5000)
        if row_count >= medium_threshold:
            if save_owner:
                write_gate_timeout = _gate_env_float("SUMMARY_WRITE_GATE_TIMEOUT_SAVE_OWNER_HUGE", 20.0)
                skip_if_busy = _gate_env_bool("SUMMARY_SAVE_OWNER_HUGE_SKIP_IF_BUSY", False)
                retry = max(int(retry), _gate_env_int("SUMMARY_UPSERT_RETRY_SAVE_OWNER_HUGE_MIN", 8))
                sleep_base = min(float(sleep_base), _gate_env_float("SUMMARY_UPSERT_SLEEP_SAVE_OWNER_HUGE_MAX", 0.20))
            else:
                write_gate_timeout = _gate_env_float("SUMMARY_WRITE_GATE_TIMEOUT_HUGE", 0.25)
                skip_if_busy = True
                retry = min(int(retry), _gate_env_int("SUMMARY_UPSERT_RETRY_HUGE_MAX", 2))
                sleep_base = min(float(sleep_base), _gate_env_float("SUMMARY_UPSERT_SLEEP_HUGE_MAX", 0.10))
        elif write_gate_timeout is None:
            if iv == 1:
                write_gate_timeout = _gate_env_float("SUMMARY_WRITE_GATE_TIMEOUT_1M", 8.0)
                skip_if_busy = bool(skip_if_busy)
            else:
                write_gate_timeout = _gate_env_float("SUMMARY_WRITE_GATE_TIMEOUT_OTHER", 0.25)
                skip_if_busy = True
                retry = min(int(retry), _gate_env_int("SUMMARY_UPSERT_RETRY_OTHER_MAX", 2))
                sleep_base = min(float(sleep_base), _gate_env_float("SUMMARY_UPSERT_SLEEP_OTHER_MAX", 0.15))

        logger.debug(
            "[SUMMARY WRITE GATE] execute_upsert interval=%s table=%s rows=%s timeout=%s "
            "skip_if_busy=%s retry=%s sleep=%.3f save_owner=%s entry_only=%s",
            iv, table_for_log, row_count, write_gate_timeout, skip_if_busy,
            int(retry), float(sleep_base), save_owner, entry_only,
        )

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
