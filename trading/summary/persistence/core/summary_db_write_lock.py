# ============================================================
# File   : trading/summary/persistence/core/summary_db_write_lock.py
# Version: Ver1.0-PRODUCTION-SUMMARY-DB-WRITE-LOCK
# ------------------------------------------------------------
# 【概要】
#   summaryYYYYMMDD.db への書き込みを DB単位で直列化する
#   SQLite の database is locked 対策用グローバル write lock
#
# 【目的】
#   - stock_summary_1min / 3min / 5min が別テーブルでも、
#     同じ SQLite DB ファイルに対する同時 writer を防ぐ
#   - interval別 lock だけでは防げない DB ファイル単位の
#     write lock 競合を抑止する
#   - 起動時 bootstrap / recovery / periodic summary / yahoo補完などの
#     保存処理が重なっても DB 書き込みを 1本化する
#
# 【重要】
#   SQLite は同一DBファイルへの同時書き込みが基本1 writerのみ。
#   よって table別・interval別ではなく DB単位の直列化が必要。
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


# ============================================================
# global DB write lock
# ============================================================

_summary_db_write_lock = threading.RLock()


@contextmanager
def summary_db_write_lock(
    *,
    reason: str = "",
    interval: Optional[int] = None,
    table_name: str = "",
    timeout_sec: Optional[float] = None,
) -> Iterator[float]:
    """
    summary DB 書き込み用の DB単位 lock。

    Parameters
    ----------
    reason:
        ログ用の保存理由。
    interval:
        1, 3, 5 など。ログ用。
    table_name:
        stock_summary_1min など。ログ用。
    timeout_sec:
        None の場合は無期限待機。
        秒数指定時は、その時間だけ待つ。

    Yields
    ------
    lock_wait_sec:
        lock取得までに待った秒数。

    Notes
    -----
    SQLite は同じDBファイルへの write transaction を同時に複数持てない。
    そのため interval別 lock だけでは不十分。

    例:
      - 1min 保存中
      - 3min 保存中
      - 5min 保存中
      - 起動時 recovery 保存中
      - yahoo complement 保存中

    これらが同一 summaryYYYYMMDD.db に同時書き込みすると
    database is locked が発生する。
    """
    started = time.monotonic()
    acquired = False

    try:
        if timeout_sec is None:
            _summary_db_write_lock.acquire()
            acquired = True
        else:
            acquired = _summary_db_write_lock.acquire(timeout=float(timeout_sec))

        wait = time.monotonic() - started

        if not acquired:
            raise TimeoutError(
                f"summary db write lock timeout interval={interval} "
                f"table={table_name} reason={reason} timeout={timeout_sec}"
            )

        if wait >= 1.0:
            logger.warning(
                "[SUMMARY DB LOCK] acquired after wait interval=%s table=%s "
                "reason=%s wait=%.3fs tid=%s thread=%s",
                interval,
                table_name,
                reason,
                wait,
                threading.get_ident(),
                threading.current_thread().name,
            )
        else:
            logger.debug(
                "[SUMMARY DB LOCK] acquired interval=%s table=%s reason=%s "
                "wait=%.3fs tid=%s thread=%s",
                interval,
                table_name,
                reason,
                wait,
                threading.get_ident(),
                threading.current_thread().name,
            )

        yield wait

    finally:
        if acquired:
            try:
                _summary_db_write_lock.release()
                logger.debug(
                    "[SUMMARY DB LOCK] released interval=%s table=%s reason=%s "
                    "elapsed=%.3fs tid=%s thread=%s",
                    interval,
                    table_name,
                    reason,
                    time.monotonic() - started,
                    threading.get_ident(),
                    threading.current_thread().name,
                )
            except Exception:
                logger.exception(
                    "[SUMMARY DB LOCK] release failed interval=%s table=%s reason=%s",
                    interval,
                    table_name,
                    reason,
                )


__all__ = [
    "summary_db_write_lock",
]