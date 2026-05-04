# ============================================================
# AI/sell_guard.py
# (Ver26-FINAL-SELL-ASYMMETRIC)
# ============================================================

def allow_sell_entry(row: dict) -> bool:
    """
    SELL 専用の追加ガード
    BUY とは非対称
    """

    # MA75 未完成は不可
    if not row.get("ma_ready_75", False):
        return False

    price = row.get("close_price", 0)
    ma75  = row.get("ma75", 0)
    vwap  = row.get("vwap", price)

    # 高値圏（MA75 上）では売らない
    if price > ma75:
        return False

    # VWAP からの乖離が弱いと戻り売りにならない
    if price > vwap * 0.995:
        return False

    # RSI が低すぎる＝売られ過ぎは追撃しない
    rsi = row.get("rsi", 50)
    if rsi < 25:
        return False

    return True
