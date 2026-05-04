# ============================================================
# trading/entry/order_strategy.py
# Ver1.0-ORDER-STRATEGY
# ============================================================

import time

# 初動判定（秒）
INITIAL_MARKET_SEC = 30

def decide_order_type(entry_info: dict) -> str:
    """
    return:
      - "MARKET"
      - "LIMIT"
    """
    created_at = entry_info.get("created_at")
    if not created_at:
        return "MARKET"

    elapsed = time.time() - created_at.timestamp()

    if elapsed <= INITIAL_MARKET_SEC:
        return "MARKET"

    return "LIMIT"
