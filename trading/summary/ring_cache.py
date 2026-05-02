# ============================================================
# trading/summary/ring_cache.py
# Ver1.0-LOCK-SAFE-MULTI-SYMBOL-PRODUCTION-FINAL
# ------------------------------------------------------------
# ✔ interval別リングバッファ
# ✔ symbol別管理
# ✔ O(1) append
# ✔ dequeベース
# ✔ スレッド安全
# ✔ DataFrame変換対応
# ✔ 本番低遅延設計
# ============================================================

from __future__ import annotations

import threading
from collections import deque
from typing import Dict, List, Optional
import pandas as pd


# ============================================================
# 設定
# ============================================================

DEFAULT_MAXLEN = {
    1: 300,   # 1分足 5時間分程度
    3: 200,
    5: 200,
}


# ============================================================
# 内部構造
# ============================================================

class RingCache:

    def __init__(self):

        self._lock = threading.Lock()

        # interval -> symbol -> deque
        self._cache: Dict[int, Dict[str, deque]] = {}

    # ========================================================
    # 初期化
    # ========================================================

    def _ensure_interval(self, interval: int):

        if interval not in self._cache:
            self._cache[interval] = {}

    def _ensure_symbol(self, interval: int, symbol: str):

        self._ensure_interval(interval)

        if symbol not in self._cache[interval]:
            maxlen = DEFAULT_MAXLEN.get(interval, 300)
            self._cache[interval][symbol] = deque(maxlen=maxlen)

    # ========================================================
    # 追加
    # ========================================================

    def append(self, interval: int, row: dict):

        symbol = row.get("symbol")
        if symbol is None:
            return

        with self._lock:
            self._ensure_symbol(interval, symbol)
            self._cache[interval][symbol].append(row)

    # ========================================================
    # 複数追加（DataFrame対応）
    # ========================================================

    def append_df(self, interval: int, df: pd.DataFrame):

        if df is None or df.empty:
            return

        for _, row in df.iterrows():
            self.append(interval, row.to_dict())

    # ========================================================
    # 直近取得
    # ========================================================

    def last(self, interval: int, symbol: str) -> Optional[dict]:

        with self._lock:

            if interval not in self._cache:
                return None

            if symbol not in self._cache[interval]:
                return None

            dq = self._cache[interval][symbol]
            if not dq:
                return None

            return dq[-1]

    # ========================================================
    # 直近N取得
    # ========================================================

    def last_n(self, interval: int, symbol: str, n: int) -> List[dict]:

        with self._lock:

            if interval not in self._cache:
                return []

            if symbol not in self._cache[interval]:
                return []

            dq = self._cache[interval][symbol]
            if len(dq) < n:
                return list(dq)

            return list(dq)[-n:]

    # ========================================================
    # 全symbol取得
    # ========================================================

    def get_all_symbols(self, interval: int) -> List[str]:

        with self._lock:

            if interval not in self._cache:
                return []

            return list(self._cache[interval].keys())

    # ========================================================
    # DataFrame変換
    # ========================================================

    def to_dataframe(self, interval: int, symbol: str) -> pd.DataFrame:

        with self._lock:

            if interval not in self._cache:
                return pd.DataFrame()

            if symbol not in self._cache[interval]:
                return pd.DataFrame()

            dq = self._cache[interval][symbol]

            if not dq:
                return pd.DataFrame()

            return pd.DataFrame(list(dq))

    # ========================================================
    # interval全体をDataFrame化
    # ========================================================

    def interval_to_dataframe(self, interval: int) -> pd.DataFrame:

        with self._lock:

            if interval not in self._cache:
                return pd.DataFrame()

            rows = []

            for symbol, dq in self._cache[interval].items():
                rows.extend(list(dq))

            if not rows:
                return pd.DataFrame()

            return pd.DataFrame(rows)

    # ========================================================
    # リセット
    # ========================================================

    def reset(self):

        with self._lock:
            self._cache.clear()

    # ========================================================
    # intervalリセット
    # ========================================================

    def reset_interval(self, interval: int):

        with self._lock:
            if interval in self._cache:
                self._cache[interval].clear()


# ============================================================
# シングルトン
# ============================================================

ring_cache = RingCache()