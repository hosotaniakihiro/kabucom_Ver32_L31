# ============================================================
# AI/confidence.py
# ============================================================

MARKET_FACTOR = {
    "STRONG_BULL": {"BUY": 1.15, "SELL": 0.70},
    "BULL":        {"BUY": 1.00, "SELL": 0.85},
    "RANGE":       {"BUY": 0.85, "SELL": 0.85},
    "BEAR":        {"BUY": 0.70, "SELL": 1.00},
    "STRONG_BEAR": {"BUY": 0.60, "SELL": 1.15},
}


def calc_confidence_buy(row, market_regime):
    if not row["indicator_ready"]:
        return 0.0
    conf = 1.0
    if not row["ma_ready_75"]:
        conf *= 0.5
    conf *= MARKET_FACTOR[market_regime]["BUY"]
    return round(conf, 3)


def calc_confidence_sell(row, market_regime):
    if not row["indicator_ready"] or not row["ma_ready_75"]:
        return 0.0
    conf = 1.0
    if row["close_price"] > row.get("ma75", float("inf")):
        conf *= 0.5
    conf *= MARKET_FACTOR[market_regime]["SELL"]
    return round(conf, 3)
