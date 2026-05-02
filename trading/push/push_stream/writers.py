# ============================================================
# File   : trading/push/push_stream/writers.py
# Version: Ver1.1-PRODUCTION-PUSH-STREAM-WRITERS-SINGLETON
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
# 【主な機能】
#   ✔ queue put
#   ✔ batch flush
#   ✔ StreamDBWriter.add_push_row 対応
#   ✔ add_row 互換対応
#   ✔ flush() 対応
#   ✔ OrderBook writer 連携維持
#   ✔ flush profile log
#   ✔ runtime flag 更新
#   ✔ production safe
# ============================================================

from __future__ import annotations

import logging
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
# writer init
# ============================================================

def _init_stream_writer() -> Any:
    """
    StreamDBWriter を取得する。

    重要:
      startup 側で trading.push.push_db_writer.stream_writer singleton を
      起動しているため、ここでも同じ singleton を優先使用する。

    これをしないと、
      startup 側 writer
      push_stream 側 writer
    が分裂し、起動状態や flush 経路が不安定になる。
    """
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
            logger.warning(
                "[push_stream] singleton stream_writer missing -> created new StreamDBWriter"
            )
            return writer

    except Exception:
        logger.exception("[push_stream] StreamDBWriter init failed")

    return None


def _init_order_book_writer() -> Any:
    """
    OrderBook writer を取得する。

    StreamDBWriter 側にも OrderBookDBWriter が統合されているが、
    旧互換のため push_stream 側の order_book_writer も維持する。
    """
    try:
        if OrderBookDBWriter is not None:
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
    """
    if not isinstance(row, dict):
        state._total_dropped += 1
        logger.warning(
            "[push_stream] queue put skipped: row is not dict total_dropped=%d",
            state._total_dropped,
        )
        return

    try:
        state._push_queue.put_nowait(row)
    except queue.Full:
        state._total_dropped += 1
        logger.warning(
            "[push_stream] queue full -> dropped total=%d",
            state._total_dropped,
        )


# ============================================================
# flush
# ============================================================

def _flush_rows(rows: List[dict]) -> bool:
    if not rows:
        return True

    ok = True

    try:
        if state._stream_writer is None:
            logger.error("[push_stream] stream writer missing rows=%d", len(rows))
            return False

        added = 0

        for row in rows:
            try:
                if hasattr(state._stream_writer, "add_push_row"):
                    state._stream_writer.add_push_row(row)
                    added += 1
                elif hasattr(state._stream_writer, "add_row"):
                    state._stream_writer.add_row(row)
                    added += 1
                else:
                    ok = False
                    logger.error(
                        "[push_stream] stream writer has no add method writer=%s",
                        type(state._stream_writer).__name__,
                    )
                    break
            except Exception:
                ok = False
                logger.exception(
                    "[push_stream] stream writer add row failed symbol=%s",
                    row.get("symbol"),
                )

        if ok and hasattr(state._stream_writer, "flush"):
            result = state._stream_writer.flush()
            if result is False:
                ok = False

        logger.debug(
            "[push_stream] stream writer batch add rows=%d added=%d ok=%s",
            len(rows),
            added,
            ok,
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
        logger.info(
            "[push_stream] flushed %d rows -> stream_data total_flushed=%d",
            len(rows),
            state._total_flushed,
        )
    else:
        logger.error("[push_stream] flush failed rows=%d", len(rows))

    return ok


def _flush_worker() -> None:
    _safe_set_runtime("push_writer_running", True)
    logger.info("[push_stream] flush worker started")

    batch: List[dict] = []
    last_flush_ts = time.time()

    while not state._stop_event.is_set():
        try:
            try:
                row = state._push_queue.get(timeout=max(0.1, FLUSH_INTERVAL_SEC / 2))
                batch.append(row)
            except queue.Empty:
                pass

            now_ts = time.time()
            should_flush = (
                len(batch) >= FLUSH_BATCH_SIZE
                or bool(batch and (now_ts - last_flush_ts) >= FLUSH_INTERVAL_SEC)
            )

            if should_flush:
                _flush_rows(batch)
                batch = []
                last_flush_ts = now_ts

        except Exception:
            logger.exception("[push_stream] flush worker loop failed")
            time.sleep(0.5)

    try:
        if batch:
            _flush_rows(batch)
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