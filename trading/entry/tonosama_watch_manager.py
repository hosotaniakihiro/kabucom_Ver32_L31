# ============================================================
# tonosama_watch_manager.py
# 殿様イナゴ 監視銘柄維持レイヤー
# ============================================================

import datetime as dt
from global_state import global_data

WATCH_TTL_SEC = 120  # 2分維持


def update_watch(
    symbol: str,
    price: float,
    volume_speed: float,
    fast_ret: float,
    now: dt.datetime,
):
    """
    ENTRY しなくても監視を続けるための登録
    （ENTRY条件より弱い）
    """

    if volume_speed < 3000:
        return
    if fast_ret < 0.10:
        return

    global_data.tonosama_watch[symbol] = {
        "price": price,
        "volume_speed": volume_speed,
        "fast_ret": fast_ret,
        "first_seen": global_data.tonosama_watch.get(
            symbol, {}
        ).get("first_seen", now),
        "last_seen": now,
    }
def cleanup_watch(now: dt.datetime):
    """
    TTL 超過した監視銘柄を削除
    """
    expired = []

    for sym, info in global_data.tonosama_watch.items():
        if (now - info["last_seen"]).total_seconds() > WATCH_TTL_SEC:
            expired.append(sym)

    for sym in expired:
        del global_data.tonosama_watch[sym]
