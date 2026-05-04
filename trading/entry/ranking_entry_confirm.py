# trading/entry/ranking_entry_confirm.py（新規でもOK）

import datetime as dt
import logging
from global_state import global_data

logger = logging.getLogger(__name__)


def confirm_ranking_entry_on_push(symbol: str, price: float, volume: float):
    """
    PUSH 到着時にランキング pending を確定させる
    """

    pending = getattr(global_data, "pending_entries", {})
    info = pending.get(symbol)

    if not info:
        return

    now = dt.datetime.now()

    cond = info["entry_conditions"]

    # --- expire ---
    if now >= cond["expire_at"]:
        pending.pop(symbol, None)
        logger.info(f"[RANK CONFIRM] expired {symbol}")
        return

    # --- volume 条件 ---
    if volume < cond.get("min_volume_speed", 0):
        return

    # --- 確定 ---
    pending.pop(symbol, None)

    logger.warning(
        f"[RANK CONFIRM] ENTRY {symbol} "
        f"{'BUY' if info['is_buy'] else 'SELL'} "
        f"vol={volume}"
    )

    # 👉 ここで ENTRY 実行（既存の仕組みに合わせる）
    from trading.entry.entry_from_summary import register_entry_from_summary

    row = {
        "symbol": symbol,
        "symbolname": info["symbolname"],
        "entry_decision": "BUY" if info["is_buy"] else "SELL",
        "price": price,
        "source": "ranking",
        "reason": info["reason"],
    }

    register_entry_from_summary(row)
