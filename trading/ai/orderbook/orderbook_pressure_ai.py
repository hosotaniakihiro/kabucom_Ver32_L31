# ============================================================
# trading/ai/orderbook/orderbook_pressure_ai.py
#
# ORDERBOOK PRESSURE AI
#
# Detects microstructure signals:
#
#   orderbook pressure
#   liquidity imbalance
#   absorption
#   spoofing risk
#   orderbook collapse
#   spread compression
#
# Designed for high-frequency microstructure analysis
# ============================================================

from __future__ import annotations

import logging
import math
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe(v):

    try:

        f = float(v)

        if not math.isfinite(f):
            return None

        return f

    except Exception:

        return None


def _clip(x, lo=0.0, hi=1.0):

    try:
        x = float(x)
    except Exception:
        return 0.0

    if not math.isfinite(x):
        return 0.0

    return max(lo, min(x, hi))


def _safe_div(a, b):

    try:

        a = float(a)
        b = float(b)

        if b == 0:
            return 0.0

        r = a / b

        if not math.isfinite(r):
            return 0.0

        return r

    except Exception:

        return 0.0


# ============================================================
# Pressure
# ============================================================

def _orderbook_pressure(row):

    bid = _safe(row.get("bid_volume"))
    ask = _safe(row.get("ask_volume"))

    if bid is None or ask is None:

        return 0

    total = bid + ask

    if total == 0:
        return 0

    imbalance = (bid - ask) / total

    if imbalance > 0.5:
        return 3

    if imbalance > 0.25:
        return 2

    if imbalance > 0.1:
        return 1

    if imbalance < -0.5:
        return -3

    if imbalance < -0.25:
        return -2

    if imbalance < -0.1:
        return -1

    return 0


# ============================================================
# Liquidity imbalance
# ============================================================

def _liquidity_imbalance(row):

    bid_depth = _safe(row.get("bid_depth"))
    ask_depth = _safe(row.get("ask_depth"))

    if bid_depth is None or ask_depth is None:

        return 0

    ratio = _safe_div(bid_depth, ask_depth)

    if ratio > 3:
        return 2

    if ratio > 2:
        return 1

    if ratio < 0.33:
        return -2

    if ratio < 0.5:
        return -1

    return 0


# ============================================================
# Absorption
# ============================================================

def _absorption(row):

    trade_volume = _safe(row.get("trade_volume"))
    bid_volume = _safe(row.get("bid_volume"))
    ask_volume = _safe(row.get("ask_volume"))

    if trade_volume is None or bid_volume is None or ask_volume is None:

        return 0

    book = bid_volume + ask_volume

    if book == 0:

        return 0

    ratio = trade_volume / book

    if ratio > 1.5:

        return 3

    if ratio > 1:

        return 2

    if ratio > 0.7:

        return 1

    return 0


# ============================================================
# Spread compression
# ============================================================

def _spread_compression(row):

    spread = _safe(row.get("spread"))
    spread_avg = _safe(row.get("spread_avg"))

    if spread is None or spread_avg is None or spread_avg == 0:

        return 0

    ratio = spread / spread_avg

    if ratio < 0.5:

        return 2

    if ratio < 0.75:

        return 1

    return 0


# ============================================================
# Book collapse
# ============================================================

def _book_collapse(row):

    bid_drop = _safe(row.get("bid_drop"))
    ask_drop = _safe(row.get("ask_drop"))

    if bid_drop is None or ask_drop is None:

        return 0

    if bid_drop > 0.6:

        return -2

    if ask_drop > 0.6:

        return 2

    return 0


# ============================================================
# Algo wall detection
# ============================================================

def _algo_wall(row):

    wall_size = _safe(row.get("wall_size"))
    volume = _safe(row.get("volume"))

    if wall_size is None or volume is None or volume == 0:

        return 0

    ratio = wall_size / volume

    if ratio > 5:

        return 2

    if ratio > 3:

        return 1

    return 0


# ============================================================
# Compute score
# ============================================================

def compute_orderbook_pressure_score(row):

    score = 0
    reasons = {}

    funcs = {

        "orderbook_pressure": _orderbook_pressure,
        "liquidity_imbalance": _liquidity_imbalance,
        "absorption": _absorption,
        "spread_compression": _spread_compression,
        "book_collapse": _book_collapse,
        "algo_wall": _algo_wall,

    }

    for name, func in funcs.items():

        try:

            s = func(row)

            if s:

                score += s
                reasons[name] = s

        except Exception:

            logger.exception(f"orderbook signal failed: {name}")

    return score, reasons


# ============================================================
# Signal classification
# ============================================================

def classify_orderbook_signal(row):

    score, reasons = compute_orderbook_pressure_score(row)

    if score >= 4:

        return "STRONG_BUY_PRESSURE", score, reasons

    if score >= 2:

        return "BUY_PRESSURE", score, reasons

    if score <= -4:

        return "STRONG_SELL_PRESSURE", score, reasons

    if score <= -2:

        return "SELL_PRESSURE", score, reasons

    return "NEUTRAL", score, reasons


# ============================================================
# DataFrame API
# ============================================================

def apply_orderbook_pressure_ai(df: pd.DataFrame):

    if df is None or df.empty:

        return df

    try:

        df = df.copy()

        scores = []
        signals = []

        for row in df.to_dict("records"):

            signal, score, _ = classify_orderbook_signal(row)

            scores.append(score)
            signals.append(signal)

        df["orderbook_pressure_score"] = scores
        df["orderbook_signal"] = signals

        return df

    except Exception:

        logger.exception("orderbook_pressure_ai failure")

        return df


# ============================================================
# Compatibility API
# ============================================================

def calc_orderbook_pressure_score(row: pd.Series) -> int:

    score = 0

    bid = row.get("bid_volume", 0)
    ask = row.get("ask_volume", 0)

    try:

        bid = float(bid)
        ask = float(ask)

    except Exception:

        return 0

    total = bid + ask

    if total == 0:

        return 0

    imbalance = (bid - ask) / total

    if imbalance > 0.3:

        score += 3

    if imbalance > 0.5:

        score += 5

    if imbalance < -0.3:

        score -= 3

    if imbalance < -0.5:

        score -= 5

    return score