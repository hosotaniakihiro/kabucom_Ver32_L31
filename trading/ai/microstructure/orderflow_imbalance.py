# ============================================================
# trading/ai/microstructure/orderflow_imbalance.py
# PRODUCTION ORDERFLOW IMBALANCE ENGINE
#
# Computes:
#   Orderflow imbalance
#   Trade imbalance
#   Liquidity imbalance
#   Flow momentum
#   Aggressive order ratio
#
# Designed for real-time microstructure analysis
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd
import logging
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_div(a, b):
    if b == 0:
        return 0.0
    return float(a) / float(b)


# ============================================================
# Core Orderflow Imbalance
# ============================================================

def compute_orderflow_imbalance(
    bid_volume: float,
    ask_volume: float
) -> float:

    total = bid_volume + ask_volume

    if total <= 0:
        return 0.0

    return (bid_volume - ask_volume) / total


# ============================================================
# Trade Imbalance
# ============================================================

def compute_trade_imbalance(trades: pd.DataFrame) -> float:

    if trades is None or len(trades) == 0:
        return 0.0

    buy = trades.loc[trades["side"] == "BUY", "size"].sum()
    sell = trades.loc[trades["side"] == "SELL", "size"].sum()

    total = buy + sell

    if total == 0:
        return 0.0

    return (buy - sell) / total


# ============================================================
# Liquidity Imbalance
# ============================================================

def compute_liquidity_imbalance(board_snapshot: Dict) -> float:

    bids = board_snapshot.get("bids", [])
    asks = board_snapshot.get("asks", [])

    bid_vol = sum([lvl["size"] for lvl in bids])
    ask_vol = sum([lvl["size"] for lvl in asks])

    return compute_orderflow_imbalance(bid_vol, ask_vol)


# ============================================================
# Delta Volume
# ============================================================

def compute_delta_volume(trades: pd.DataFrame) -> float:

    if trades is None or len(trades) == 0:
        return 0.0

    buy_vol = trades.loc[trades["side"] == "BUY", "size"].sum()
    sell_vol = trades.loc[trades["side"] == "SELL", "size"].sum()

    return float(buy_vol - sell_vol)


# ============================================================
# Flow Momentum
# ============================================================

def compute_flow_momentum(trades: pd.DataFrame) -> float:

    if trades is None or len(trades) < 3:
        return 0.0

    delta_series = []

    for i in range(len(trades)):

        side = trades.iloc[i]["side"]
        size = trades.iloc[i]["size"]

        if side == "BUY":
            delta_series.append(size)
        else:
            delta_series.append(-size)

    momentum = np.sum(delta_series)

    norm = np.sum(np.abs(delta_series))

    if norm == 0:
        return 0.0

    return momentum / norm


# ============================================================
# Aggressive Order Ratio
# ============================================================

def compute_aggressive_ratio(trades: pd.DataFrame) -> float:

    if trades is None or len(trades) == 0:
        return 0.0

    if "aggressor" not in trades.columns:
        return 0.0

    aggressive = trades.loc[trades["aggressor"] == True]

    return _safe_div(len(aggressive), len(trades))


# ============================================================
# Full Orderflow Analysis
# ============================================================

class OrderflowAnalyzer:

    def analyze(
        self,
        trades: pd.DataFrame,
        board_snapshot: Dict
    ) -> Dict:

        try:

            trade_imbalance = compute_trade_imbalance(trades)

            liquidity_imbalance = compute_liquidity_imbalance(
                board_snapshot
            )

            delta_volume = compute_delta_volume(trades)

            flow_momentum = compute_flow_momentum(trades)

            aggressive_ratio = compute_aggressive_ratio(trades)

            flow_score = (
                trade_imbalance * 0.35
                + liquidity_imbalance * 0.25
                + flow_momentum * 0.25
                + aggressive_ratio * 0.15
            )

            return {

                "trade_imbalance": float(trade_imbalance),

                "liquidity_imbalance": float(liquidity_imbalance),

                "delta_volume": float(delta_volume),

                "flow_momentum": float(flow_momentum),

                "aggressive_ratio": float(aggressive_ratio),

                "orderflow_score": float(flow_score),

            }

        except Exception:

            logger.exception("OrderflowAnalyzer failure")

            return {
                "orderflow_score": 0.0
            }


# ============================================================
# Singleton
# ============================================================

_analyzer = None


def get_orderflow_analyzer() -> OrderflowAnalyzer:

    global _analyzer

    if _analyzer is None:

        _analyzer = OrderflowAnalyzer()

    return _analyzer