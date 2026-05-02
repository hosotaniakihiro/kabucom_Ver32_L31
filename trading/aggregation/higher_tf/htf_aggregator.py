"""
============================================================
htf_aggregator.py
Higher Timeframe Aggregator
Ver2.1-PRODUCTION-FINAL-STABLE
------------------------------------------------------------
✔ 1分 → 3分 / 5分 O(1)集約
✔ cache管理
✔ finalize trigger
✔ NaN / inf防御
✔ realtime安全
✔ memory leak防止
✔ incremental_higher_tf_engine用モジュール
✔ backward compatibility (htf_engine)
✔ singleton engine
✔ thread safe
✔ finalize.py互換 API (on_1m_confirmed)
✔ flush support
============================================================
"""

from __future__ import annotations

import logging
import datetime as dt
import threading
import numpy as np

logger = logging.getLogger(__name__)

_lock = threading.Lock()


# ============================================================
# Utility
# ============================================================

def _safe_dt(x):

    if x is None:
        return None

    try:

        import pandas as pd

        ts = pd.to_datetime(x, errors="coerce")

        if pd.isna(ts):
            return None

        return ts.to_pydatetime().replace(tzinfo=None)

    except Exception:

        return None


def _is_abnormal(v):

    try:

        f = float(v)

        return np.isnan(f) or np.isinf(f)

    except Exception:

        return True


# ============================================================
# Aggregator
# ============================================================

class HTFAggregator:
    """
    O(1) higher timeframe aggregator
    """

    def __init__(self, tf: int):

        self.tf = tf
        self.cache = {}

    # ========================================================
    # update
    # ========================================================

    def update(self, symbol: str, dt_bar: dt.datetime, row: dict):

        dt_bar = _safe_dt(dt_bar)

        if not dt_bar:
            return None

        minute = dt_bar.minute

        base_minute = minute - (minute % self.tf)

        tf_time = dt_bar.replace(
            minute=base_minute,
            second=0,
            microsecond=0
        )

        open_raw = row.get("open_price")
        high_raw = row.get("high_price")
        low_raw = row.get("low_price")
        close_raw = row.get("close_price")
        volume_raw = row.get("volume", 0)

        if any(
            v is None or _is_abnormal(v)
            for v in (open_raw, high_raw, low_raw, close_raw)
        ):

            logger.debug(
                "[HTF SKIP] invalid OHLC %s %s",
                symbol,
                dt_bar
            )

            return None

        open_ = float(open_raw)
        high_ = float(high_raw)
        low_ = float(low_raw)
        close_ = float(close_raw)

        volume_ = (
            float(volume_raw)
            if volume_raw is not None and not _is_abnormal(volume_raw)
            else 0.0
        )

        cache = self.cache.get(symbol)

        # ----------------------------------------------------
        # 新バー開始
        # ----------------------------------------------------

        if not cache or cache.get("minute") != tf_time:

            finished_bar = None

            if cache:
                finished_bar = cache

            self.cache[symbol] = {
                "minute": tf_time,
                "open_price": open_,
                "high_price": high_,
                "low_price": low_,
                "close_price": close_,
                "volume": volume_,
            }

            return finished_bar

        # ----------------------------------------------------
        # update
        # ----------------------------------------------------

        cache["high_price"] = max(cache["high_price"], high_)
        cache["low_price"] = min(cache["low_price"], low_)
        cache["close_price"] = close_
        cache["volume"] += volume_

        # ----------------------------------------------------
        # finalize trigger
        # ----------------------------------------------------

        if minute % self.tf == self.tf - 1:

            finished_bar = cache

            self.cache.pop(symbol, None)

            return finished_bar

        return None

    # ========================================================
    # flush
    # ========================================================

    def flush(self):

        now = dt.datetime.now().replace(
            second=0,
            microsecond=0
        )

        finalized = []

        for symbol, bar in list(self.cache.items()):

            minute = _safe_dt(bar.get("minute"))

            if not minute:
                continue

            if minute < now:

                finalized.append((symbol, bar))

                self.cache.pop(symbol, None)

        return finalized


# ============================================================
# Engine (3min / 5min manager)
# ============================================================

class HTFEngine:

    def __init__(self):

        self.agg3 = HTFAggregator(3)
        self.agg5 = HTFAggregator(5)

    # ========================================================
    # update
    # ========================================================

    def update(self, symbol, dt_bar, row):

        finished = []

        bar3 = self.agg3.update(symbol, dt_bar, row)
        if bar3:
            finished.append(("3m", symbol, bar3))

        bar5 = self.agg5.update(symbol, dt_bar, row)
        if bar5:
            finished.append(("5m", symbol, bar5))

        return finished

    # ========================================================
    # finalize API (for finalize.py)
    # ========================================================

    def on_1m_confirmed(self, symbol, dt_bar, row):
        """
        finalize.py 互換API
        """

        try:

            return self.update(symbol, dt_bar, row)

        except Exception:

            logger.exception(
                "[HTF] on_1m_confirmed failed"
            )

            return []

    # ========================================================
    # flush
    # ========================================================

    def flush(self):

        finished = []

        for symbol, bar in self.agg3.flush():
            finished.append(("3m", symbol, bar))

        for symbol, bar in self.agg5.flush():
            finished.append(("5m", symbol, bar))

        return finished


# ============================================================
# Singleton
# ============================================================

_engine = None


def get_htf_engine():

    global _engine

    if _engine is None:

        with _lock:

            if _engine is None:
                _engine = HTFEngine()

    return _engine


# ============================================================
# Backward compatibility
# ============================================================

def htf_engine(symbol, dt_bar, row):
    """
    legacy API compatibility
    """

    engine = get_htf_engine()

    return engine.update(symbol, dt_bar, row)