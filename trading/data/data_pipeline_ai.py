# ============================================================
# trading/data/data_pipeline_ai.py
#
# AI DATA PIPELINE
#
# Real-time data ingestion pipeline
#
# Handles
#
#   tick ingestion
#   orderbook updates
#   bar aggregation
#   feature generation
#   AI signal preparation
#
# ============================================================

from __future__ import annotations

import logging
import time
from typing import Dict, List

import pandas as pd
import numpy as np

from trading.ai.feature_store_ai import get_feature_store_ai
from trading.system.institutional_trading_system_architecture import (
    get_institutional_trading_system
)

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe(v):

    try:

        f = float(v)

        if not np.isfinite(f):

            return 0.0

        return f

    except Exception:

        return 0.0


# ============================================================
# Data Pipeline
# ============================================================

class DataPipelineAI:

    def __init__(self):

        self.feature_store = get_feature_store_ai()

        self.system = get_institutional_trading_system()

        self.tick_buffer: Dict[str, List[Dict]] = {}

        self.bar_cache: Dict[str, pd.DataFrame] = {}

        self.bar_window = 60

    # --------------------------------------------------------
    # Tick ingestion
    # --------------------------------------------------------

    def ingest_tick(self, symbol: str, tick: Dict):

        try:

            if symbol not in self.tick_buffer:

                self.tick_buffer[symbol] = []

            tick["timestamp"] = time.time()

            self.tick_buffer[symbol].append(tick)

            if len(self.tick_buffer[symbol]) > self.bar_window:

                self.tick_buffer[symbol].pop(0)

        except Exception:

            logger.exception("Tick ingestion failed")

    # --------------------------------------------------------
    # Orderbook update
    # --------------------------------------------------------

    def update_orderbook(self, symbol: str, book: Dict):

        try:

            features = {

                "bid_volume": _safe(book.get("bid_volume")),

                "ask_volume": _safe(book.get("ask_volume")),

                "spread": _safe(book.get("spread"))

            }

            self.feature_store.update_features(symbol, features)

        except Exception:

            logger.exception("Orderbook update failed")

    # --------------------------------------------------------
    # Build bar
    # --------------------------------------------------------

    def build_bar(self, symbol: str):

        try:

            ticks = self.tick_buffer.get(symbol)

            if not ticks:

                return None

            df = pd.DataFrame(ticks)

            bar = {

                "open": df["price"].iloc[0],

                "high": df["price"].max(),

                "low": df["price"].min(),

                "close": df["price"].iloc[-1],

                "volume": df.get("size", pd.Series()).sum()

            }

            return bar

        except Exception:

            logger.exception("Bar build failed")

            return None

    # --------------------------------------------------------
    # Update bar cache
    # --------------------------------------------------------

    def update_bar(self, symbol: str):

        try:

            bar = self.build_bar(symbol)

            if bar is None:

                return

            if symbol not in self.bar_cache:

                self.bar_cache[symbol] = pd.DataFrame()

            df = self.bar_cache[symbol]

            df = pd.concat([

                df,

                pd.DataFrame([bar])

            ])

            self.bar_cache[symbol] = df.tail(500)

        except Exception:

            logger.exception("Bar update failed")

    # --------------------------------------------------------
    # Generate AI signals
    # --------------------------------------------------------

    def generate_ai_signals(self, symbol: str):

        try:

            df = self.bar_cache.get(symbol)

            if df is None or len(df) < 20:

                return None

            df = self.system.process_market_data(df)

            latest = df.iloc[-1].to_dict()

            self.feature_store.update_features(

                symbol,

                latest

            )

            signals = {

                "price": latest.get("close"),

                "spread": latest.get("spread"),

                "bid_volume": latest.get("bid_volume"),

                "ask_volume": latest.get("ask_volume"),

                "algo_spike_score": latest.get("algo_spike_score"),

                "toxicity": latest.get("toxicity"),

                "price_df": df

            }

            return signals

        except Exception:

            logger.exception("Signal generation failed")

            return None

    # --------------------------------------------------------
    # Pipeline step
    # --------------------------------------------------------

    def step(self, symbol: str):

        try:

            self.update_bar(symbol)

            signals = self.generate_ai_signals(symbol)

            if signals is None:

                return None

            entry, risk = self.system.evaluate_entry(signals)

            exit_signal = self.system.evaluate_exit(signals)

            return {

                "entry": entry,

                "risk": risk,

                "exit": exit_signal

            }

        except Exception:

            logger.exception("Pipeline step failed")

            return None


# ============================================================
# Singleton
# ============================================================

_pipeline = None


def get_data_pipeline_ai():

    global _pipeline

    if _pipeline is None:

        _pipeline = DataPipelineAI()

    return _pipeline