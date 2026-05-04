# trading/handlers/exit_instant.py

from global_state import global_data

LOSS_CUT_PCT = -0.3
TRAIL_BACK_PCT = -0.2


def check_instant_exit(symbol: str, price: float):
    info = global_data.position_runtime.get(symbol)
    if not info:
        return None

    entry = info["entry_price"]
    side = info["side"]
    peak = info["peak_price"]

    if side == "BUY":
        pnl_pct = (price - entry) / entry * 100
        info["peak_price"] = max(peak, price)

        # 即ロスカット
        if pnl_pct <= LOSS_CUT_PCT:
            return f"INSTANT_STOP {pnl_pct:.2f}%"

        # トレーリング利確
        trail_pct = (price - info["peak_price"]) / info["peak_price"] * 100
        if info["peak_price"] > entry and trail_pct <= TRAIL_BACK_PCT:
            return f"TRAIL_TP {trail_pct:.2f}%"

    else:  # SELL
        pnl_pct = (entry - price) / entry * 100
        info["peak_price"] = min(peak, price)

        if pnl_pct <= LOSS_CUT_PCT:
            return f"INSTANT_STOP {pnl_pct:.2f}%"

        trail_pct = (info["peak_price"] - price) / info["peak_price"] * 100
        if info["peak_price"] < entry and trail_pct <= TRAIL_BACK_PCT:
            return f"TRAIL_TP {trail_pct:.2f}%"

    return None
