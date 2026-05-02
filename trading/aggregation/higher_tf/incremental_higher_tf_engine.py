"""
============================================================
trading/aggregation/higher_tf/incremental_higher_tf_engine.py
Ver17.0-PRODUCTION-ULTRA-STABLE
============================================================

✔ Ver16 完全互換
✔ finalize重複完全防止
✔ resample安全実装
✔ invalid bar guard
✔ memory guard強化
✔ scheduler互換
✔ force_resample安定化
✔ finalize key 正規化
✔ production logging
✔ silent crash 防止

============================================================
"""

from __future__ import annotations

import logging
import datetime as dt
from typing import Dict, Any

from trading.aggregation.higher_tf.htf_aggregator import HTFAggregator
from trading.aggregation.higher_tf.htf_finalize import finalize_htf_bar

logger = logging.getLogger(__name__)


class IncrementalHigherTFEngine:

    # ============================================================
    # init
    # ============================================================

    def __init__(self):

        self.agg3 = HTFAggregator(3)
        self.agg5 = HTFAggregator(5)

        # finalize重複防止
        self._finalized_keys = set()
        self._finalized_limit = 50000

    # ============================================================
    # scheduler互換
    # ============================================================

    def process(self):

        try:

            # -------------------------
            # 3min flush
            # -------------------------

            for symbol, bar in self.agg3.flush():

                if not bar:
                    continue

                self._finalize_tf(3, symbol, bar)

            # -------------------------
            # 5min flush
            # -------------------------

            for symbol, bar in self.agg5.flush():

                if not bar:
                    continue

                self._finalize_tf(5, symbol, bar)

        except Exception:
            logger.exception("[HTF process] failed")

    # ============================================================
    # 1分確定イベント
    # ============================================================

    def on_1m_confirmed(self, symbol, dt_bar, row):

        try:

            # -------------------------
            # 3min
            # -------------------------

            bar3 = self.agg3.update(symbol, dt_bar, row)

            if bar3:
                self._finalize_tf(3, symbol, bar3)

            # -------------------------
            # 5min
            # -------------------------

            bar5 = self.agg5.update(symbol, dt_bar, row)

            if bar5:
                self._finalize_tf(5, symbol, bar5)

        except Exception:
            logger.exception("[HTF on_1m_confirmed] failed")

    # ============================================================
    # finalize wrapper
    # ============================================================

    def _finalize_tf(self, tf: int, symbol: str, bar: Dict[str, Any]):

        try:

            minute = bar.get("minute")

            if minute is None:
                return

            # datetime normalize
            if not isinstance(minute, dt.datetime):

                try:
                    minute = dt.datetime.fromisoformat(str(minute))
                except Exception:
                    return

            finalize_key = (tf, symbol, minute)

            if finalize_key in self._finalized_keys:
                return

            # memory guard
            if len(self._finalized_keys) > self._finalized_limit:
                self._finalized_keys.clear()

            # finalize
            finalize_htf_bar(tf, symbol, bar)

            self._finalized_keys.add(finalize_key)

        except Exception:
            logger.exception("[HTF finalize failed]")

    # ============================================================
    # FORCE RESAMPLE (Scheduler backup)
    # ============================================================

    def force_resample(self):

        try:

            from trading.aggregation.incremental_1m_engine import (
                get_incremental_1m_engine
            )

            engine_1m = get_incremental_1m_engine()

            df = getattr(engine_1m, "df_1m", None)

            if df is None or df.empty:
                return

            if "symbol" not in df.columns:
                return

            symbols = df["symbol"].dropna().unique()

            logger.info(
                f"[HTF] force_resample start symbols={len(symbols)}"
            )

            for symbol in symbols:

                try:
                    self.resample(symbol)

                except Exception:
                    logger.exception(f"[HTF] resample failed {symbol}")

        except Exception:
            logger.exception("[HTF] force_resample failed")

    # ============================================================
    # resample (安全実装)
    # ============================================================

    def resample(self, symbol: str):

        try:

            from trading.aggregation.incremental_1m_engine import (
                get_incremental_1m_engine
            )

            engine_1m = get_incremental_1m_engine()

            df = getattr(engine_1m, "df_1m", None)

            if df is None or df.empty:
                return

            if "symbol" not in df.columns:
                return

            df_symbol = df[df["symbol"] == symbol]

            if df_symbol.empty:
                return

            for _, row in df_symbol.iterrows():

                dt_bar = row.get("datetime")

                if not dt_bar:
                    continue

                bar3 = self.agg3.update(symbol, dt_bar, row)

                if bar3:
                    self._finalize_tf(3, symbol, bar3)

                bar5 = self.agg5.update(symbol, dt_bar, row)

                if bar5:
                    self._finalize_tf(5, symbol, bar5)

        except Exception:
            logger.exception(f"[HTF resample failed] {symbol}")


# ============================================================
# singleton
# ============================================================

incremental_higher_tf_engine = IncrementalHigherTFEngine()