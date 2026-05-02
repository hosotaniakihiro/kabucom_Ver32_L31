# ============================================================
# trading/summary/push_ring_buffer.py
# Ver1.0-PRODUCTION-RING-BUFFER
# ------------------------------------------------------------
# ✔ realtime_engine専用
# ✔ thread-safe
# ✔ flush型
# ✔ memory制御
# ============================================================

from __future__ import annotations
import threading
import pandas as pd


class PushRingBuffer:

    def __init__(self, max_size: int = 10000):
        self._lock = threading.Lock()
        self._buffer = []
        self._max_size = max_size

    # --------------------------------------------------------
    # append
    # --------------------------------------------------------
    def append(self, row: dict):
        if not row:
            return

        with self._lock:
            self._buffer.append(row)

            # 上限超えたら前を削る
            if len(self._buffer) > self._max_size:
                self._buffer = self._buffer[-self._max_size:]

    # --------------------------------------------------------
    # flush
    # --------------------------------------------------------
    def flush(self) -> pd.DataFrame:
        with self._lock:
            if not self._buffer:
                return pd.DataFrame()

            data = self._buffer
            self._buffer = []

        return pd.DataFrame(data)


# ------------------------------------------------------------
# シングルトン
# ------------------------------------------------------------
push_buffer = PushRingBuffer()