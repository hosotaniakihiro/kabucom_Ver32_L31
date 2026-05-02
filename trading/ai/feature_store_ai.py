# ============================================================
# trading/ai/feature_store_ai.py
#
# AI FEATURE STORE
#
# Central feature storage for AI models
#
# Responsibilities
#
#   feature aggregation
#   feature validation
#   history storage
#   model input generation
#
# ============================================================

from __future__ import annotations

import logging
import math
import time
import threading
from typing import Dict, List, Any, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_float(v: Any) -> float:

    try:

        f = float(v)

        if not math.isfinite(f):

            return 0.0

        return f

    except Exception:

        return 0.0


def _safe_series(series: Optional[pd.Series]) -> pd.Series:

    if series is None:

        return pd.Series(dtype=float)

    s = pd.to_numeric(series, errors="coerce")

    s = s.replace([np.inf, -np.inf], np.nan)

    return s.fillna(0)


def _safe_dict(features: Dict[str, Any]) -> Dict[str, float]:

    safe = {}

    for k, v in features.items():

        safe[k] = _safe_float(v)

    return safe


# ============================================================
# Feature Store
# ============================================================

class FeatureStoreAI:

    def __init__(self):

        # ----------------------------------
        # live cache
        # ----------------------------------

        self.cache: Dict[str, Dict[str, float]] = {}

        # ----------------------------------
        # feature history
        # ----------------------------------

        self.history: Dict[str, List[Dict[str, float]]] = {}

        # ----------------------------------
        # limits
        # ----------------------------------

        self.max_history = 2000

        # ----------------------------------
        # thread safety
        # ----------------------------------

        self._lock = threading.Lock()

        # ----------------------------------
        # stats
        # ----------------------------------

        self.update_count = 0

        logger.info("[FEATURE STORE] initialized")

    # --------------------------------------------------------
    # Update features
    # --------------------------------------------------------

    def update_features(self, symbol: str, features: Dict[str, Any]):

        try:

            if not symbol:

                return

            safe_features = _safe_dict(features)

            safe_features["timestamp"] = time.time()

            with self._lock:

                # update cache

                self.cache[symbol] = safe_features

                # update history

                if symbol not in self.history:

                    self.history[symbol] = []

                self.history[symbol].append(safe_features)

                # limit history

                if len(self.history[symbol]) > self.max_history:

                    self.history[symbol] = self.history[symbol][-self.max_history:]

                self.update_count += 1

        except Exception:

            logger.exception("[FEATURE STORE] update failed")

    # --------------------------------------------------------
    # Get latest features
    # --------------------------------------------------------

    def get_features(self, symbol: str) -> Dict[str, float]:

        try:

            with self._lock:

                return self.cache.get(symbol, {}).copy()

        except Exception:

            return {}

    # --------------------------------------------------------
    # Get history
    # --------------------------------------------------------

    def get_history(self, symbol: str, window: int = 50) -> pd.DataFrame:

        try:

            with self._lock:

                hist = self.history.get(symbol, [])

                if not hist:

                    return pd.DataFrame()

                df = pd.DataFrame(hist)

            if window:

                df = df.tail(window)

            return df

        except Exception:

            logger.exception("[FEATURE STORE] history failure")

            return pd.DataFrame()

    # --------------------------------------------------------
    # Build model feature vector
    # --------------------------------------------------------

    def build_feature_vector(self, symbol: str) -> Dict[str, float]:

        try:

            f = self.get_features(symbol)

            if not f:

                return {}

            vector = {

                # -------------------------
                # scoring AI
                # -------------------------

                "ranking_score": _safe_float(
                    f.get("ranking_momentum_score")
                ),

                "algo_spike": _safe_float(
                    f.get("algo_spike_score")
                ),

                "vwap_score": _safe_float(
                    f.get("vwap_deviation_score")
                ),

                # -------------------------
                # institutional
                # -------------------------

                "institutional_buy": _safe_float(
                    f.get("institutional_buy_score")
                ),

                "institutional_sell": _safe_float(
                    f.get("institutional_sell_score")
                ),

                # -------------------------
                # orderbook
                # -------------------------

                "orderbook_pressure": _safe_float(
                    f.get("orderbook_pressure_score")
                ),

                # -------------------------
                # regime
                # -------------------------

                "regime_score": _safe_float(
                    f.get("regime_score")
                ),

                # -------------------------
                # microstructure
                # -------------------------

                "volatility": _safe_float(
                    f.get("volatility")
                ),

                "liquidity": _safe_float(
                    f.get("liquidity")
                ),

                "spread": _safe_float(
                    f.get("spread")
                ),

                # -------------------------
                # optional extensions
                # -------------------------

                "momentum": _safe_float(
                    f.get("momentum")
                ),

                "volume_spike": _safe_float(
                    f.get("volume_spike")
                ),

                "tick_speed": _safe_float(
                    f.get("tick_speed")
                ),

            }

            return vector

        except Exception:

            logger.exception("[FEATURE STORE] vector build failed")

            return {}

    # --------------------------------------------------------
    # Build dataframe for ML models
    # --------------------------------------------------------

    def build_feature_dataframe(
        self,
        symbol: str,
        window: int = 100
    ) -> pd.DataFrame:

        try:

            df = self.get_history(symbol, window)

            if df.empty:

                return df

            numeric_cols = df.select_dtypes(
                include=[np.number]
            ).columns

            for col in numeric_cols:

                df[col] = _safe_series(df[col])

            df = df.reset_index(drop=True)

            return df

        except Exception:

            logger.exception("[FEATURE STORE] dataframe build failed")

            return pd.DataFrame()

    # --------------------------------------------------------
    # Clear symbol cache
    # --------------------------------------------------------

    def clear_symbol(self, symbol: str):

        try:

            with self._lock:

                if symbol in self.cache:

                    del self.cache[symbol]

                if symbol in self.history:

                    del self.history[symbol]

        except Exception:

            pass

    # --------------------------------------------------------
    # global stats
    # --------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:

        try:

            with self._lock:

                return {

                    "symbols": len(self.cache),

                    "updates": self.update_count,

                    "history_symbols": len(self.history)

                }

        except Exception:

            return {}

    # ============================================================
    # Tick ingest
    # ============================================================

    def ingest_tick(self, symbol: str, tick: dict):

        """
        kabu push tick を FeatureStore に格納
        """

        try:

            if not symbol or tick is None:
                return

            price = tick.get("price")
            volume = tick.get("volume")

            if price is None:
                return

            if not hasattr(self, "tick_buffer"):
                self.tick_buffer = {}

            buf = self.tick_buffer.setdefault(symbol, [])

            buf.append(
                {
                    "price": float(price),
                    "volume": float(volume or 0),
                }
            )

            # メモリ肥大防止
            if len(buf) > 200:
                buf.pop(0)

        except Exception:
            logger.exception("FeatureStore ingest_tick failed")
# ============================================================
# Singleton
# ============================================================

_ai: Optional[FeatureStoreAI] = None


def get_feature_store_ai() -> FeatureStoreAI:

    global _ai

    if _ai is None:

        _ai = FeatureStoreAI()

    return _ai