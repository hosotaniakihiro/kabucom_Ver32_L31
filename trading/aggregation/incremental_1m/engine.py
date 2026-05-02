"""
============================================================
engine.py
Incremental1MEngine Core
------------------------------------------------------------
✔ incremental_1m モジュール中核
✔ unconfirmed bar 復元
✔ history cache 管理
✔ finalize 重複防止
✔ processor / finalize モジュール連携
✔ StreamOrchestrator process互換
✔ force finalize 対応
✔ finalize guard 強化
✔ finalize key TTL cleanup
✔ minute型安全化
✔ invalid bar guard（NEW）
✔ OHLC sanitize（NEW）
✔ finalize safety guard（NEW）
✔ HFT production stable
============================================================
"""

from __future__ import annotations

import logging
import datetime as dt
from typing import Dict, Tuple, Optional

from trading.aggregation.unconfirmed_store import (
    load_all as load_unconfirmed,
)

from .history_cache import HistoryCache
from .processor import process_row
from .finalize import finalize_bar


logger = logging.getLogger(__name__)


# ============================================================
# UTIL
# ============================================================

def _normalize_minute(v) -> Optional[dt.datetime]:

    if v is None:
        return None

    if isinstance(v, dt.datetime):
        return v.replace(second=0, microsecond=0)

    try:
        return dt.datetime.fromisoformat(str(v)).replace(
            second=0,
            microsecond=0
        )
    except Exception:
        return None


def _sanitize_price(v):

    try:
        if v is None:
            return None

        v = float(v)

        if v <= 0:
            return None

        return v

    except Exception:
        return None


def _is_invalid_bar(bar: dict) -> bool:
    """
    invalid OHLC guard
    """

    try:

        o = _sanitize_price(bar.get("open"))
        h = _sanitize_price(bar.get("high"))
        l = _sanitize_price(bar.get("low"))
        c = _sanitize_price(bar.get("close"))

        if c is None:
            return True

        if o is None or h is None or l is None:
            return True

        if o == 0 and h == 0 and l == 0:
            return True

        return False

    except Exception:

        return True


# ============================================================
# ENGINE
# ============================================================

class Incremental1MEngine:
    """
    1分足インクリメンタルエンジン
    """

    def __init__(self):

        # --------------------------------------------------
        # unconfirmed bar restore
        # --------------------------------------------------

        try:

            cache = load_unconfirmed()

            if cache:
                self.current_bar_cache: Dict[str, dict] = cache
            else:
                self.current_bar_cache = {}

        except Exception:

            logger.exception("[1M] unconfirmed restore failed")

            self.current_bar_cache = {}

        # --------------------------------------------------
        # history cache
        # --------------------------------------------------

        self.history_cache = HistoryCache()

        # --------------------------------------------------
        # finalize guard
        # --------------------------------------------------

        self._finalized_keys: set[Tuple[str, dt.datetime]] = set()
        self._last_cleanup = dt.datetime.now()

        # --------------------------------------------------
        # higher TF engine
        # --------------------------------------------------

        try:

            from trading.aggregation.incremental_higher_tf_engine import (
                incremental_higher_tf_engine
            )

            self.higher_tf_engine = incremental_higher_tf_engine

        except Exception:

            self.higher_tf_engine = None

        logger.info(
            "[1M ENGINE] initialized cache_size=%s",
            len(self.current_bar_cache)
        )

    # ========================================================
    # STREAM LOOP
    # ========================================================

    def process(self):

        try:

            self.force_time_based_finalize()

            if self.higher_tf_engine:

                try:

                    self.higher_tf_engine.process()

                except Exception:

                    logger.exception("[1M] HTF process failed")

            self._cleanup_finalize_keys()

        except Exception:

            logger.exception("[1M] process loop failed")

    # ========================================================
    # PUBLIC ENTRY
    # ========================================================

    def process_row(self, row: dict):

        try:

            process_row(self, row)

        except Exception:

            logger.exception("[1M] process_row failed")

    # ========================================================
    # SAFE FINALIZE
    # ========================================================

    def safe_finalize(self, symbol: str, cache: dict):

        try:

            self._finalize_bar(symbol, cache)

        except Exception:

            logger.exception("[1M] finalize failed")

    # ========================================================
    # FINALIZE CORE
    # ========================================================

    def _finalize_bar(self, symbol: str, bar: dict):

        minute = _normalize_minute(bar.get("minute"))

        if minute is None:
            return

        finalize_key = (symbol, minute)

        if finalize_key in self._finalized_keys:
            return

        # ----------------------------------------------
        # invalid bar guard
        # ----------------------------------------------

        if _is_invalid_bar(bar):

            logger.debug(
                "[1M] invalid bar skipped %s %s",
                symbol,
                minute
            )

            return

        try:

            finalize_bar(self, symbol, bar)

        except Exception:

            logger.exception("[1M] finalize_bar crashed")

            return

        self._finalized_keys.add(finalize_key)

        logger.info(
            "[1M CONFIRMED] %s %s",
            symbol,
            minute
        )

    # ========================================================
    # FORCE FINALIZE
    # ========================================================

    def force_time_based_finalize(self):

        try:

            now = dt.datetime.now().replace(
                second=0,
                microsecond=0
            )

            for symbol in list(self.current_bar_cache.keys()):

                cache = self.current_bar_cache.get(symbol)

                if not cache:
                    continue

                bar_minute = _normalize_minute(cache.get("minute"))

                if bar_minute is None:
                    continue

                if now > bar_minute + dt.timedelta(minutes=1):

                    try:

                        self._finalize_bar(symbol, cache)

                    except Exception:

                        logger.exception(
                            "[1M] force finalize failed"
                        )

                    finally:

                        self.current_bar_cache.pop(
                            symbol,
                            None
                        )

        except Exception:

            logger.exception(
                "[1M] force_time_based_finalize crashed"
            )

    # ========================================================
    # FINALIZE KEY CLEANUP
    # ========================================================

    def _cleanup_finalize_keys(self):

        try:

            now = dt.datetime.now()

            if (now - self._last_cleanup).seconds < 600:
                return

            cutoff = now - dt.timedelta(hours=2)

            self._finalized_keys = {
                k for k in self._finalized_keys
                if k[1] > cutoff
            }

            self._last_cleanup = now

        except Exception:

            logger.exception("[1M] finalize key cleanup failed")


# ============================================================
# Singleton
# ============================================================

_incremental_1m_engine = None


def get_incremental_1m_engine():

    global _incremental_1m_engine

    if _incremental_1m_engine is None:

        _incremental_1m_engine = Incremental1MEngine()

    return _incremental_1m_engine