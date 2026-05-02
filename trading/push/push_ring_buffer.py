# ============================================================
# trading/push/push_ring_buffer.py
# Ver3.0-ABSOLUTE-LOCKSAFE-DF-FAST-FINAL
# ------------------------------------------------------------
# ✔ 固定長リングバッファ
# ✔ DataFrame即取得
# ✔ datetime完全保証
# ✔ symbol抽出対応
# ✔ max_rows制御
# ✔ スレッド安全
# ✔ dict混入完全防止
# ✔ 差分処理高速化
# ✔ pandas最適化
# ============================================================

from __future__ import annotations

import threading
import pandas as pd
from collections import deque
from typing import Iterable, Optional
import logging

logger = logging.getLogger(__name__)


# ============================================================
# PushRingBuffer
# ============================================================

class PushRingBuffer:

    def __init__(self, maxlen: int = 200000):
        """
        maxlen:
            保持する最大ティック数
            （例: 200,000 ≒ 数時間分）
        """
        self._buffer = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._maxlen = maxlen

    # ========================================================
    # append（単体）
    # ========================================================

    def append(self, data: dict):

        if not isinstance(data, dict):
            return

        if "datetime" not in data or "symbol" not in data:
            return

        try:
            data["datetime"] = pd.to_datetime(
                data["datetime"],
                errors="coerce",
            )
        except Exception:
            return

        if pd.isna(data["datetime"]):
            return

        with self._lock:
            self._buffer.append(data)

    # ========================================================
    # extend（複数）
    # ========================================================

    def extend(self, rows: Iterable[dict]):

        if rows is None:
            return

        with self._lock:
            for r in rows:
                if not isinstance(r, dict):
                    continue
                if "datetime" not in r or "symbol" not in r:
                    continue
                try:
                    r["datetime"] = pd.to_datetime(
                        r["datetime"],
                        errors="coerce",
                    )
                except Exception:
                    continue
                if pd.isna(r["datetime"]):
                    continue
                self._buffer.append(r)

    # ========================================================
    # DataFrame取得（全件）
    # ========================================================

    def to_dataframe(
        self,
        *,
        symbols: Optional[set[str]] = None,
        since: Optional[pd.Timestamp] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:

        with self._lock:
            if not self._buffer:
                return pd.DataFrame()

            data = list(self._buffer)

        df = pd.DataFrame(data)

        if df.empty:
            return df

        # ----------------------------------------------------
        # datetime保証
        # ----------------------------------------------------
        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce",
        )

        df = df.dropna(subset=["datetime"])

        # ----------------------------------------------------
        # symbolフィルタ
        # ----------------------------------------------------
        if symbols:
            df = df[df["symbol"].astype(str).isin(symbols)]

        # ----------------------------------------------------
        # sinceフィルタ
        # ----------------------------------------------------
        if since is not None:
            since = pd.to_datetime(since)
            df = df[df["datetime"] > since]

        # ----------------------------------------------------
        # 行制限
        # ----------------------------------------------------
        if max_rows is not None and len(df) > max_rows:
            df = df.tail(max_rows)

        df = df.sort_values("datetime").reset_index(drop=True)

        return df

    # ========================================================
    # 最新datetime取得
    # ========================================================

    def latest_datetime(self) -> Optional[pd.Timestamp]:

        with self._lock:
            if not self._buffer:
                return None

            last = self._buffer[-1]

        try:
            return pd.to_datetime(last.get("datetime"))
        except Exception:
            return None

    # ========================================================
    # size
    # ========================================================

    def __len__(self):
        with self._lock:
            return len(self._buffer)

    # ========================================================
    # clear
    # ========================================================

    def clear(self):
        with self._lock:
            self._buffer.clear()

    # ========================================================
    # debug info
    # ========================================================

    def info(self) -> dict:
        with self._lock:
            size = len(self._buffer)

        return {
            "size": size,
            "maxlen": self._maxlen,
        }

    # ============================================================
    # シングルトン
    # ============================================================

push_ring_buffer = PushRingBuffer()