# ============================================================
# File   : utils/sqlite_ultra_fast_writer.py
# Version: Ver3.0-ULTRA-FAST-SQLITE-WRITER
# ------------------------------------------------------------
# ✔ WAL optimized
# ✔ busy_timeout
# ✔ batch writer
# ✔ retry on locked
# ✔ queue buffer
# ✔ executemany
# ✔ thread safe
# ✔ high throughput
# ============================================================

from __future__ import annotations

import sqlite3
import threading
import logging
import time
from queue import Queue, Empty

logger = logging.getLogger(__name__)


# ============================================================
# SQLITE ULTRA FAST WRITER
# ============================================================

class SQLiteUltraFastWriter:

    def __init__(
        self,
        db_path: str,
        batch_size: int = 500,
        flush_interval: float = 0.5
    ):

        self.db_path = db_path
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self.queue = Queue()
        self.running = False

        self.thread = None

        self.conn = None


    # ========================================================
    # connection
    # ========================================================

    def _connect(self):

        self.conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            check_same_thread=False
        )

        cur = self.conn.cursor()

        # WAL mode
        cur.execute("PRAGMA journal_mode=WAL")

        # speed tuning
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-200000")

        # lock wait
        cur.execute("PRAGMA busy_timeout=5000")


    # ========================================================
    # start writer
    # ========================================================

    def start(self):

        if self.running:
            return

        self._connect()

        self.running = True

        self.thread = threading.Thread(
            target=self._worker,
            daemon=True
        )

        self.thread.start()

        logger.info("[SQLITE WRITER] started")


    # ========================================================
    # stop writer
    # ========================================================

    def stop(self):

        self.running = False

        if self.thread:
            self.thread.join(timeout=3)

        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

        logger.info("[SQLITE WRITER] stopped")


    # ========================================================
    # enqueue write
    # ========================================================

    def write(self, query: str, data):

        self.queue.put((query, data))


    # ========================================================
    # worker
    # ========================================================

    def _worker(self):

        buffer = []

        last_flush = time.time()

        while self.running:

            try:

                item = self.queue.get(timeout=0.1)

                buffer.append(item)

            except Empty:
                pass

            now = time.time()

            if (
                len(buffer) >= self.batch_size
                or (buffer and now - last_flush > self.flush_interval)
            ):

                self._flush(buffer)

                buffer = []

                last_flush = now


    # ========================================================
    # flush
    # ========================================================

    def _flush(self, items):

        if not items:
            return

        cur = self.conn.cursor()

        grouped = {}

        for query, data in items:

            grouped.setdefault(query, []).append(data)

        for query, rows in grouped.items():

            retries = 5

            for i in range(retries):

                try:

                    cur.executemany(query, rows)

                    self.conn.commit()

                    break

                except sqlite3.OperationalError as e:

                    if "locked" in str(e):

                        logger.warning(
                            "[SQLITE WRITER] locked retry %s",
                            i + 1
                        )

                        time.sleep(0.1)

                    else:
                        raise

        logger.debug("[SQLITE WRITER] flushed rows=%s", len(items))