# ============================================================
# File   : trading/push/push_stream/writers.py
# Version: Ver1.3-PRODUCTION-PUSH-STREAM-WRITERS-MAIN-MEMORY-ONLY-GUARD
# ------------------------------------------------------------
# 【概要】
#   push_stream package 用 writer / queue / flush worker
#
# 【目的】
#   - on_message で正規化された row を queue に投入
#   - flush worker が queue から batch を取り出して stream_data DB へ保存
#   - startup 側で起動済みの trading.push.push_db_writer.stream_writer singleton を優先使用
#   - StreamDBWriter のインスタンス分裂を防止
#
# 【重要修正 Ver1.3】
#   - main.py / main_database.py 分離運用時、main.py側ではqueue投入をskip
#   - main.py側はPUSHをメモリ/price cache/5秒足更新だけに使用
#   - flush workerが誤起動しても _flush_rows 側でDB保存をno-op
#
# 【重要修正 Ver1.2】
#   - stream_writer が None のとき、flush失敗で batch を捨てない
#   - writer を遅延再初期化してから再flushする
#   - flush失敗時は取得済み batch を queue に戻す
#   - total_flushed=0 / queue肥大化の原因をログで見える化
# ============================================================

from __future__ import annotations

import logging
import os
import queue
import time
from typing import Any, List

from .constants import FLUSH_BATCH_SIZE, FLUSH_INTERVAL_SEC
from .normalize import _is_order_book_like
from . import state
from .runtime import _now, _safe_iso, _safe_set_runtime

logger = logging.getLogger(__name__)


try:
    from trading.push.order_book_db_writer import OrderBookDBWriter
except Exception:
    OrderBookDBWriter = None


# ============================================================
# split-mode guard
# ============================================================

def _should_skip_push_stream_db_work_here() -> bool:
    """
    main.py側ではpush_stream queue/flush/DB保存を行わない。
    main_database.py 側だけがPUSH DB保存を担当する。
    """
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


# ============================================================
# writer init
# ============================================================

def _init_stream_writer() -> Any:
    """
    StreamDBWriter を取得する。

    重要:
      startup 側で trading.push.push_db_writer.stream_writer singleton を
      起動しているため、ここでも同じ singleton を優先使用する。
    """
    if _should_skip_push_stream_db_work_here():
        logger.warning(
            "[push_stream] stream writer init skipped in main memory-only mode; main_database.py handles PUSH DB storage"
        )
        return None

    try:
        from trading.push import push_db_writer as mod

        writer = getattr(mod, "stream_writer", None)
        if writer is not None:
            logger.info(
                "[push_stream] use singleton stream_writer writer=%s",
                type(writer).__name__,
            )
            return writer

        cls = getattr(mod, "StreamDBWriter", None)
        if callable(cls):
            writer = cls()
            try:
                # 実装によっては start() が必要。
                start = getattr(writer, "start", None)
                if callable(start):
                    start()
            except Exception:
                logger.debug("[push_stream] created StreamDBWriter start skipped/failed", exc_info=True)
            logger.warning(
                "[push_stream] singleton stream_writer missing -> created new StreamDBWriter"
            )
            return writer

    except Exception:
        logger.exception("[push_stream] StreamDBWriter init failed")

    return None


def _ensure_stream_writer() -> Any:
    """flush時点で writer が無ければ再取得する。"""
    if _should_skip_push_stream_db_work_here():
        logger.debug("[push_stream] ensure stream writer skipped in main memory-only mode")
        return None

    try:
        if state._stream_writer is not None:
            return state._stream_writer
        writer = _init_stream_writer()
        state._stream_writer = writer
        if writer is None:
            logger.error("[push_stream] stream writer still unavailable after re-init")
        else:
            logger.warning("[push_stream] stream writer re-initialized writer=%s", type(writer).__name__)
        return writer
    except Exception:
        logger.exception("[push_stream] ensure stream writer failed")
        return None


def _init_order_book_writer() -> Any:
    """
    OrderBook writer を取得する。
    """
    if _should_skip_push_stream_db_work_here():
        logger.warning(
            "[push_stream] order book writer init skipped in main memory-only mode; main_database.py handles PUSH DB storage"
        )
        return None

    try:
        if OrderBookDBWriter is not None:
            # ------------------------------------------------------------
            # 2026-09-03 P0: OrderBook writer が 100 行ごとに commit していた。
            #
            # 本番実測 (12:51 後場、SMB):
            #   push DB   executemany 500 行 = 0.098s / commit = 8.0s
            #   OrderBook 100 行ごとに commit -> 1000 行の flush で
            #             [OrderBookDB] flushed rows=100 が 10 本以上並び、
            #             これが flush サイクル全体を律速していた
            #             (flushed_per_min が 0 のまま queue が 5,451 まで増加)。
            # commit のコストは行数にほぼ依存しないので、まとめる。
            # ------------------------------------------------------------
            try:
                _ob_buf = int(os.getenv("PUSH_ORDER_BOOK_BUFFER_SIZE", "1000"))
            except Exception:
                _ob_buf = 1000
            try:
                writer = OrderBookDBWriter(buffer_size=max(1, _ob_buf))
            except TypeError:
                writer = OrderBookDBWriter()
            try:
                start = getattr(writer, "start", None)
                if callable(start):
                    start()
            except Exception:
                logger.debug("[push_stream] order book writer start skipped", exc_info=True)
            return writer
    except Exception:
        logger.exception("[push_stream] OrderBookDBWriter init failed")

    return None


# ============================================================
# queue
# ============================================================

def _queue_put(row: dict) -> None:
    """
    on_message から呼ばれる queue 投入口。
    main.py memory-only mode ではDB保存用queueへ投入しない。
    """
    if not isinstance(row, dict):
        state._total_dropped += 1
        logger.warning(
            "[push_stream] queue put skipped: row is not dict total_dropped=%d",
            state._total_dropped,
        )
        return

    if _should_skip_push_stream_db_work_here():
        # PUSHは既に latest_price_cache / 5秒足 / DataFrame に反映済み。
        # DB保存用queueだけ投入しない。
        _safe_set_runtime("push_stream_memory_only", True)
        _safe_set_runtime("push_stream_db_queue_skipped", True)
        return

    try:
        state._push_queue.put_nowait(row)
    except queue.Full:
        state._total_dropped += 1
        logger.warning(
            "[push_stream] queue full -> dropped total=%d",
            state._total_dropped,
        )


def _requeue_rows(rows: List[dict], *, reason: str) -> int:
    """flush失敗時、取得済み batch を捨てずに queue へ戻す。"""
    if not rows:
        return 0

    if _should_skip_push_stream_db_work_here():
        logger.warning(
            "[push_stream] requeue skipped in main memory-only mode reason=%s rows=%d",
            reason,
            len(rows),
        )
        return 0

    requeued = 0
    dropped = 0
    for row in rows:
        try:
            state._push_queue.put_nowait(row)
            requeued += 1
        except queue.Full:
            dropped += 1
            state._total_dropped += 1

    logger.error(
        "[push_stream] flush batch requeued reason=%s rows=%d requeued=%d dropped=%d queue=%d total_dropped=%d",
        reason,
        len(rows),
        requeued,
        dropped,
        state._push_queue.qsize(),
        state._total_dropped,
    )
    return requeued


# ============================================================
# flush
# ============================================================

def _flush_rows(rows: List[dict]) -> bool:
    if not rows:
        return True

    if _should_skip_push_stream_db_work_here():
        _safe_set_runtime("push_stream_memory_only", True)
        _safe_set_runtime("push_writer_last_ok", True)
        logger.warning(
            "[push_stream] flush skipped in main memory-only mode rows=%d; memory/latest cache already updated",
            len(rows),
        )
        return True

    ok = True

    try:
        writer = _ensure_stream_writer()
        if writer is None:
            logger.error("[push_stream] stream writer missing rows=%d", len(rows))
            return False

        added = 0

        for row in rows:
            try:
                if hasattr(writer, "add_push_row"):
                    writer.add_push_row(row)
                    added += 1
                elif hasattr(writer, "add_row"):
                    writer.add_row(row)
                    added += 1
                else:
                    ok = False
                    logger.error(
                        "[push_stream] stream writer has no add method writer=%s",
                        type(writer).__name__,
                    )
                    break
            except Exception:
                ok = False
                logger.exception(
                    "[push_stream] stream writer add row failed symbol=%s",
                    row.get("symbol"),
                )

        if ok and hasattr(writer, "flush"):
            result = writer.flush()
            if result is False:
                ok = False

        logger.debug(
            "[push_stream] stream writer batch add rows=%d added=%d ok=%s writer=%s",
            len(rows),
            added,
            ok,
            type(writer).__name__,
        )

    except Exception:
        ok = False
        logger.exception("[push_stream] stream writer flush failed")

    try:
        if state._order_book_writer is not None:
            for row in rows:
                if not _is_order_book_like(row):
                    continue

                if hasattr(state._order_book_writer, "add_row"):
                    state._order_book_writer.add_row(row)
                elif hasattr(state._order_book_writer, "add_order_book_row"):
                    state._order_book_writer.add_order_book_row(row)
                elif hasattr(state._order_book_writer, "add_from_push_content"):
                    state._order_book_writer.add_from_push_content(
                        row.get("symbol"),
                        row.get("datetime") or row.get("current_price_time") or row.get("received_at"),
                        row,
                    )

    except Exception:
        logger.exception("[push_stream] order book writer operation failed")

    if ok:
        state._total_flushed += len(rows)
        state._last_flush_at = _now()
        _safe_set_runtime("last_push_db_flush_at", _safe_iso(state._last_flush_at))
        _safe_set_runtime("push_writer_last_ok", True)
        logger.info(
            "[push_stream] flushed %d rows -> stream_data total_flushed=%d queue=%d",
            len(rows),
            state._total_flushed,
            state._push_queue.qsize(),
        )
    else:
        _safe_set_runtime("push_writer_last_ok", False)
        logger.error("[push_stream] flush failed rows=%d queue=%d", len(rows), state._push_queue.qsize())

    return ok


def _flush_worker() -> None:
    from . import start_trace

    if _should_skip_push_stream_db_work_here():
        _safe_set_runtime("push_writer_running", False)
        start_trace.abort(5, "flush_worker_skipped_main_memory_only")
        logger.warning("[push_stream] flush worker not started in main memory-only mode")
        return

    _safe_set_runtime("push_writer_running", True)
    start_trace.trace(5, phase="worker_loop_entered", writer_ready=state._stream_writer is not None)
    logger.info("[push_stream] flush worker started")

    batch: List[dict] = []
    last_flush_ts = time.time()
    consecutive_failures = 0

    while not state._stop_event.is_set():
        try:
            try:
                row = state._push_queue.get(timeout=max(0.1, FLUSH_INTERVAL_SEC / 2))
                batch.append(row)
            except queue.Empty:
                pass

            # ------------------------------------------------------------
            # 2026-09-03 P0: queue から一気に汲む。
            #
            # 旧実装は 1 ループ 1 行しか取らないため、
            #   - 100 行の batch を作るのに 100 ループ必要
            #   - しかも直前の commit が 12 秒かかると、次のループで
            #     「時間条件」が即成立し **1 行だけの batch を commit** していた
            # 実測ログはまさに buffer=1 と buffer=100 が交互に並び、
            # 高価な commit の半分が 1 行のために使われていた。
            # ------------------------------------------------------------
            if len(batch) < FLUSH_BATCH_SIZE:
                try:
                    while len(batch) < FLUSH_BATCH_SIZE:
                        batch.append(state._push_queue.get_nowait())
                except queue.Empty:
                    pass

            now_ts = time.time()
            pending = 0
            try:
                pending = state._push_queue.qsize()
            except Exception:
                pending = 0
            should_flush = (
                len(batch) >= FLUSH_BATCH_SIZE
                or bool(batch and (now_ts - last_flush_ts) >= FLUSH_INTERVAL_SEC and pending == 0)
                or bool(batch and (now_ts - last_flush_ts) >= FLUSH_INTERVAL_SEC * 5)
            )

            if should_flush:
                ok = _flush_rows(batch)
                if ok:
                    batch = []
                    consecutive_failures = 0
                    # commit 後の時刻を使う。commit 前の now_ts を使うと、
                    # 12 秒かかった直後に時間条件が即成立して 1 行 commit になる。
                    last_flush_ts = time.time()
                else:
                    consecutive_failures += 1
                    _requeue_rows(batch, reason=f"flush_failed_{consecutive_failures}")
                    batch = []
                    last_flush_ts = time.time()
                    time.sleep(min(5.0, 0.5 * consecutive_failures))

        except Exception:
            consecutive_failures += 1
            logger.exception("[push_stream] flush worker loop failed failures=%d", consecutive_failures)
            if batch:
                _requeue_rows(batch, reason="worker_exception")
                batch = []
            time.sleep(min(5.0, 0.5 * consecutive_failures))

    try:
        if batch:
            ok = _flush_rows(batch)
            if not ok:
                _requeue_rows(batch, reason="final_flush_failed")
    except Exception:
        logger.exception("[push_stream] final flush failed")

    _safe_set_runtime("push_writer_running", False)
    logger.info("[push_stream] flush worker stopped")


__all__ = [
    "_init_stream_writer",
    "_init_order_book_writer",
    "_queue_put",
    "_flush_rows",
    "_flush_worker",
]