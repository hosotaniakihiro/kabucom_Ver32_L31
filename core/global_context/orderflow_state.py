# ============================================================
# File   : core/global_context/orderflow_state.py
# Version: V1-FINAL-ORDERFLOW-STATE
# ------------------------------------------------------------
# ✔ symbol別tick蓄積
# ✔ 30秒保持
# ✔ 3秒統計
# ✔ スレッド安全
# ✔ 内部構造隠蔽
# ============================================================

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from threading import Lock


class OrderflowState:

    def __init__(self):
        self._lock = Lock()
        self._ticks = defaultdict(list)   # symbol → tick list
        self._stats = {}                  # symbol → aggregated stats

    # ========================================================
    # tick追加
    # ========================================================
    def append_tick(self, symbol: str, tick: dict):

        now = tick["datetime"]

        with self._lock:

            buf = self._ticks[symbol]
            buf.append(tick)

            # 30秒保持
            cutoff_30 = now - dt.timedelta(seconds=30)
            self._ticks[symbol] = [
                x for x in buf if x["datetime"] >= cutoff_30
            ]

            # 3秒統計
            cutoff_3 = now - dt.timedelta(seconds=3)
            last3 = [
                x for x in self._ticks[symbol]
                if x["datetime"] >= cutoff_3
            ]

            buy_count = sum(1 for x in last3 if x["side"] == "BUY")
            sell_count = sum(1 for x in last3 if x["side"] == "SELL")

            prev = self._stats.get(symbol, {})
            prev_ask = prev.get("best_ask_size", tick.get("best_ask_size", 0))

            ask_thin = prev.get("ask_thin_count", 0)
            if tick.get("best_ask_size") and prev_ask:
                if tick["best_ask_size"] < prev_ask * 0.7:
                    ask_thin += 1

            self._stats[symbol] = {
                "buy_count_3s": buy_count,
                "sell_count_3s": sell_count,
                "best_bid_size": tick.get("best_bid_size", 0),
                "best_ask_size": tick.get("best_ask_size", 0),
                "ask_thin_count": ask_thin,
            }

    # ========================================================
    # 取得
    # ========================================================
    def get_stats(self, symbol: str):
        return self._stats.get(symbol, {})

    def clear(self):
        with self._lock:
            self._ticks.clear()
            self._stats.clear()

    def update_stats(
            self,
            symbol: str,
            buy_count_3s: int,
            sell_count_3s: int,
            best_bid_size: int,
            best_ask_size: int,
            ask_thin_count: int,
    ):
        with self._lock:
            self._stats[symbol] = {
                "buy_count_3s": buy_count_3s,
                "sell_count_3s": sell_count_3s,
                "best_bid_size": best_bid_size,
                "best_ask_size": best_ask_size,
                "ask_thin_count": ask_thin_count,
            }