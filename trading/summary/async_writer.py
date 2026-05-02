# ============================================================
# trading/summary/async_writer.py
# Ver8-PRODUCTION-ULTRA-STABLE-HYBRID-EXECUTOR
# ------------------------------------------------------------
# ✔ Ver7 完全互換
# ✔ interval別duplicate key
# ✔ DB write single worker
# ✔ interval別 single-flight
# ✔ busy skip 対応
# ✔ WAL安全
# ✔ Queue burst耐性
# ✔ graceful shutdown
# ✔ realtime_engine完全互換
# ✔ async_write_summary API維持
# ✔ batch flush安定
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import threading
import time
import queue
from typing import Optional

import pandas as pd

from concurrent.futures import ThreadPoolExecutor, Future

from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary

logger = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

MAX_QUEUE_SIZE = 10000
BATCH_SIZE = 500
FLUSH_INTERVAL_SEC = 1.0

# SQLite safety
MAX_WORKERS = 1

VALID_INTERVALS = {1, 3, 5}

# async writer 側は低優先保存
LOCK_TIMEOUT_BY_INTERVAL = {
    1: 10.0,
    3: 3.0,
    5: 3.0,
}

SKIP_IF_BUSY_BY_INTERVAL = {
    1: False,
    3: True,
    5: True,
}


# ============================================================
# duplicate key
# ============================================================

def _dedupe(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if interval == 1:
        keys = ["symbol", "datetime"]
    else:
        keys = ["symbol", "date", "time_range"]

    try:
        usable_keys = [k for k in keys if k in df.columns]
        if not usable_keys:
            return df

        return (
            df.drop_duplicates(
                subset=usable_keys,
                keep="last",
            )
            .reset_index(drop=True)
        )

    except Exception:
        logger.exception("duplicate guard failed")
        return df


def _safe_concat(df_list: list[pd.DataFrame]) -> pd.DataFrame:
    try:
        if not df_list:
            return pd.DataFrame()

        valid = [df for df in df_list if df is not None and not df.empty]
        if not valid:
            return pd.DataFrame()

        return pd.concat(valid, ignore_index=True)

    except Exception:
        logger.exception("safe concat failed")
        return pd.DataFrame()


# ============================================================
# Hybrid Async Writer
# ============================================================

class AsyncSummaryWriter:

    def __init__(self):

        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)

        self._executor = ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        )

        self._running = False
        self._thread: threading.Thread | None = None

        self._last_flush = time.time()

        self._lock = threading.Lock()

        # interval別 in-flight 管理
        self._inflight: dict[int, Optional[Future]] = {
            1: None,
            3: None,
            5: None,
        }

        # 保存中に来た追加分を interval 別に保留
        self._pending_after_inflight: dict[int, list[pd.DataFrame]] = {
            1: [],
            3: [],
            5: [],
        }


    # ========================================================
    # start
    # ========================================================

    def start(self):

        with self._lock:

            if self._running:
                return

            self._running = True

            self._thread = threading.Thread(
                target=self._worker_loop,
                daemon=True
            )

            self._thread.start()

            logger.info("🧵 AsyncSummaryWriter started")


    # ========================================================
    # stop
    # ========================================================

    def stop(self, timeout: float = 5.0):

        logger.info("🛑 AsyncSummaryWriter stopping")

        self._running = False

        if self._thread:
            self._thread.join(timeout=timeout)

        self._flush_remaining()

        self._executor.shutdown(wait=True)

        logger.info("🛑 AsyncSummaryWriter stopped")


    # ========================================================
    # API
    # ========================================================

    def async_save(self, df: pd.DataFrame, interval: int):

        if df is None or df.empty:
            return

        if interval not in VALID_INTERVALS:
            logger.warning("⚠ invalid interval=%s", interval)
            return

        self.start()

        try:
            self._queue.put((df, interval), timeout=0.1)

        except queue.Full:

            logger.warning("⚠ AsyncWriter queue full → waiting")

            try:
                self._queue.put((df, interval), timeout=1.0)
            except queue.Full:
                logger.error("❌ AsyncWriter queue overflow → drop interval=%s", interval)


    def async_write_summary(self, df: pd.DataFrame, interval: int):
        self.async_save(df, interval)


    # ========================================================
    # worker
    # ========================================================

    def _worker_loop(self):

        buffer = {
            1: [],
            3: [],
            5: [],
        }

        while self._running:

            try:
                try:
                    df, interval = self._queue.get(timeout=FLUSH_INTERVAL_SEC)
                    buffer[interval].append(df)
                except queue.Empty:
                    pass

                now = time.time()

                should_flush = (
                    any(len(v) >= BATCH_SIZE for v in buffer.values())
                    or now - self._last_flush >= FLUSH_INTERVAL_SEC
                )

                if should_flush:
                    self._flush_buffer(buffer)
                    self._last_flush = now

                self._promote_pending_if_idle()

            except Exception:
                logger.exception("AsyncWriter loop error")

        self._flush_buffer(buffer)
        self._promote_pending_if_idle(force=True)


    # ========================================================
    # flush
    # ========================================================

    def _flush_buffer(self, buffer):

        for interval, df_list in buffer.items():

            if not df_list:
                continue

            try:
                df_all = _safe_concat(df_list)
                df_all = _dedupe(df_all, interval)

                if df_all.empty:
                    continue

                # すでに同じ interval の保存が走っているなら pending に積む
                inflight = self._inflight.get(interval)
                if inflight is not None and not inflight.done():
                    self._pending_after_inflight[interval].append(df_all)
                    logger.info(
                        "[AsyncWriter] interval=%s inflight -> pending append rows=%d pending_batches=%d",
                        interval,
                        len(df_all),
                        len(self._pending_after_inflight[interval]),
                    )
                    continue

                future = self._executor.submit(
                    self._bulk_upsert_wrapper,
                    df_all,
                    interval,
                )

                self._inflight[interval] = future
                future.add_done_callback(
                    lambda fut, iv=interval: self._handle_future_done(iv, fut)
                )

            except Exception:
                logger.exception(
                    "AsyncWriter flush failed (%smin)",
                    interval
                )

            finally:
                buffer[interval].clear()


    def _promote_pending_if_idle(self, force: bool = False):

        for interval in VALID_INTERVALS:
            try:
                inflight = self._inflight.get(interval)
                pending = self._pending_after_inflight.get(interval, [])

                is_idle = (
                    inflight is None
                    or inflight.done()
                )

                if not is_idle:
                    continue

                if not pending:
                    continue

                df_all = _safe_concat(pending)
                self._pending_after_inflight[interval].clear()

                df_all = _dedupe(df_all, interval)
                if df_all.empty:
                    continue

                future = self._executor.submit(
                    self._bulk_upsert_wrapper,
                    df_all,
                    interval,
                )

                self._inflight[interval] = future
                future.add_done_callback(
                    lambda fut, iv=interval: self._handle_future_done(iv, fut)
                )

                logger.info(
                    "[AsyncWriter] interval=%s pending promoted rows=%d",
                    interval,
                    len(df_all),
                )

            except Exception:
                logger.exception(
                    "[AsyncWriter] promote pending failed interval=%s",
                    interval,
                )


    # ========================================================
    # bulk wrapper
    # ========================================================

    def _bulk_upsert_wrapper(self, df: pd.DataFrame, interval: int):

        rows = 0 if df is None else len(df)
        timeout_sec = LOCK_TIMEOUT_BY_INTERVAL.get(interval, 5.0)
        skip_if_busy = SKIP_IF_BUSY_BY_INTERVAL.get(interval, True)

        try:
            logger.info(
                "[AsyncWriter] save start interval=%s rows=%d timeout=%.1fs skip_if_busy=%s",
                interval,
                rows,
                timeout_sec,
                skip_if_busy,
            )

            try:
                return bulk_upsert_summary(
                    df,
                    interval,
                    lock_timeout_sec=timeout_sec,
                    skip_if_busy=skip_if_busy,
                )
            except TypeError:
                # 後方互換
                return bulk_upsert_summary(df, interval)

        except Exception:
            logger.exception(
                "[AsyncWriter] bulk upsert failed interval=%s rows=%d",
                interval,
                rows,
            )
            raise


    # ========================================================
    # future done
    # ========================================================

    def _handle_future_done(self, interval: int, future: Future):

        try:
            exc = future.exception()

            if exc:
                logger.exception(
                    "❌ async DB task failed",
                    exc_info=exc
                )
            else:
                try:
                    result = future.result()
                except Exception:
                    result = None

                logger.info(
                    "[AsyncWriter] save done interval=%s result=%s",
                    interval,
                    result,
                )

        except Exception:
            logger.exception("future exception check failed")

        finally:
            try:
                # 完了後ただちに pending を昇格
                self._promote_pending_if_idle()
            except Exception:
                logger.exception(
                    "[AsyncWriter] post-future promote failed interval=%s",
                    interval,
                )


    # ========================================================
    # force flush
    # ========================================================

    def _flush_remaining(self):

        temp = {
            1: [],
            3: [],
            5: [],
        }

        while not self._queue.empty():

            try:
                df, interval = self._queue.get_nowait()

                if interval in VALID_INTERVALS:
                    temp[interval].append(df)

            except Exception:
                break

        self._flush_buffer(temp)

        # pending があれば一度昇格
        self._promote_pending_if_idle(force=True)


# ============================================================
# singleton
# ============================================================

async_summary_writer = AsyncSummaryWriter()


# ============================================================
# compatibility API
# ============================================================

def async_write_summary(df: pd.DataFrame, interval: int):
    async_summary_writer.async_write_summary(df, interval)


def async_save(df: pd.DataFrame, interval: int):
    async_summary_writer.async_save(df, interval)