# ============================================================
# realtime_bar_builder.py
# ------------------------------------------------------------
# PUSH tick → realtime 1m / 3m / 5m bar builder
# ・確定バー検知
# ・確定時コールバック対応
# ============================================================

import datetime as dt
from collections import defaultdict
from typing import Callable, List


# ============================================================
# 🔹 リアルタイムバー（共通）
# ============================================================
class RealtimeBar:
    def __init__(self, start_dt: dt.datetime):
        self.start_dt = start_dt          # バー開始時刻（minute基準）
        self.end_dt: dt.datetime | None = None
        self.open_price: float | None = None
        self.high_price: float | None = None
        self.low_price: float | None = None
        self.close_price: float | None = None
        self.volume: int = 0
        self.is_closed: bool = False

    # --------------------------------------------------------
    def update(self, price: float, volume: int):
        if self.open_price is None:
            self.open_price = price
            self.high_price = price
            self.low_price = price
        else:
            self.high_price = max(self.high_price, price)
            self.low_price = min(self.low_price, price)

        self.close_price = price
        self.volume += volume

    # --------------------------------------------------------
    def close(self, end_dt: dt.datetime):
        self.end_dt = end_dt
        self.is_closed = True


# ============================================================
# 🔥 メイン：リアルタイムバー生成器
# ============================================================
class RealtimeBarBuilder:

    def __init__(self):
        # symbol → tf → current bar
        self.current_bars = defaultdict(dict)

        # symbol → tf → list[confirmed bars]
        self.confirmed_bars = defaultdict(lambda: defaultdict(list))

        # 🔔 確定バー通知用コールバック
        # callback(symbol: str, tf: int, bar: RealtimeBar)
        self._on_bar_closed_callbacks: List[Callable] = []

    # --------------------------------------------------------
    # 🔔 確定バー時コールバック登録
    # --------------------------------------------------------
    def register_on_bar_closed(self, func: Callable):
        """
        func(symbol: str, tf: int, bar: RealtimeBar) を受け取る関数
        """
        self._on_bar_closed_callbacks.append(func)

    # --------------------------------------------------------
    # 🔹 PUSH tick を流し込む
    # --------------------------------------------------------
    def on_tick(self, symbol: str, tick_dt: dt.datetime, price: float, volume: int):
        """
        tick_dt は tz-naive datetime 前提
        """

        minute_dt = tick_dt.replace(second=0, microsecond=0)

        # === 1分足 ===
        self._update_tf(symbol, 1, minute_dt, tick_dt, price, volume)

        # === 3分 / 5分足（1分足から派生）===
        for tf in (3, 5):
            tf_start = self._calc_tf_start(minute_dt, tf)
            self._update_tf(symbol, tf, tf_start, tick_dt, price, volume)

    # --------------------------------------------------------
    def _calc_tf_start(self, minute_dt: dt.datetime, tf: int) -> dt.datetime:
        """
        例：
        09:31, tf=3 → 09:30
        09:34, tf=5 → 09:30
        """
        m = minute_dt.minute
        start_minute = (m // tf) * tf
        return minute_dt.replace(minute=start_minute)

    # --------------------------------------------------------
    def _update_tf(
        self,
        symbol: str,
        tf: int,
        tf_start: dt.datetime,
        tick_dt: dt.datetime,
        price: float,
        volume: int,
    ):
        """
        tf単位のバー更新
        """

        bar = self.current_bars[symbol].get(tf)

        # --- 新しいバー開始 ---
        if bar is None or bar.start_dt != tf_start:

            # 既存バーを確定
            if bar is not None:
                bar.close(tf_start)
                self.confirmed_bars[symbol][tf].append(bar)

                # 🔔 確定バー通知
                for cb in self._on_bar_closed_callbacks:
                    try:
                        cb(symbol, tf, bar)
                    except Exception as e:
                        # コールバック例外は握りつぶす（本体を止めない）
                        print(f"❌ on_bar_closed callback error: {e}")

            # 新バー作成
            bar = RealtimeBar(tf_start)
            self.current_bars[symbol][tf] = bar

        # --- バー更新 ---
        bar.update(price, volume)

    # --------------------------------------------------------
    # 🔍 取得系 API（Entry / Exit 用）
    # --------------------------------------------------------
    def get_current_bar(self, symbol: str, tf: int) -> RealtimeBar | None:
        """未確定バー（今動いているバー）"""
        return self.current_bars[symbol].get(tf)

    def get_latest_confirmed_bar(self, symbol: str, tf: int) -> RealtimeBar | None:
        """直近の確定バー"""
        bars = self.confirmed_bars[symbol][tf]
        return bars[-1] if bars else None

    def get_confirmed_bars(self, symbol: str, tf: int, n: int | None = None):
        """確定バーをまとめて取得"""
        bars = self.confirmed_bars[symbol][tf]
        return bars[-n:] if n else bars
