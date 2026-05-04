# ============================================================
# global_data.tosama_watch 定義（殿様イナゴ監視用）
# ------------------------------------------------------------
# ・ランキング初動〜ENTRY確定までの一時監視状態
# ・PUSH / ranking / summary いずれからでも更新可能
# ・EXIT・AI・学習の共通参照点
# ============================================================

import datetime as dt


def init_tosama_watch(global_data):
    """
    global_data に tosama_watch を安全に初期化する
    """

    if hasattr(global_data, "tosama_watch"):
        return

    # --------------------------------------------------------
    # 構造:
    #
    # global_data.tosama_watch = {
    #   "7203": {
    #       "price": 2350.0,
    #       "volume_speed": 12345.0,
    #       "fast_ret": 0.32,          # [%]
    #       "first_seen": datetime,
    #       "last_update": datetime,
    #   },
    # }
    # --------------------------------------------------------
    global_data.tosama_watch = {}


def update_tosama_watch(
    global_data,
    symbol: str,
    price: float,
    volume_speed: float,
    fast_ret: float,
    now: dt.datetime | None = None,
):
    """
    殿様イナゴ監視情報を更新する
    """

    if not hasattr(global_data, "tosama_watch"):
        init_tosama_watch(global_data)

    now = now or dt.datetime.now()

    d = global_data.tosama_watch.get(symbol)

    # 初回登録
    if d is None:
        global_data.tosama_watch[symbol] = {
            "price": float(price),
            "volume_speed": float(volume_speed),
            "fast_ret": float(fast_ret),
            "first_seen": now,
            "last_update": now,
        }
        return

    # 更新
    d["price"] = float(price)
    d["volume_speed"] = float(volume_speed)
    d["fast_ret"] = float(fast_ret)
    d["last_update"] = now


def remove_tosama_watch(global_data, symbol: str):
    """
    ENTRY / EXIT / 失効時に監視対象から削除
    """
    if hasattr(global_data, "tosama_watch"):
        global_data.tosama_watch.pop(symbol, None)


def cleanup_tosama_watch(
    global_data,
    expire_seconds: int = 300,
):
    """
    一定時間更新されていない監視銘柄を自動削除
    """
    if not hasattr(global_data, "tosama_watch"):
        return

    now = dt.datetime.now()
    expired = []

    for symbol, d in global_data.tosama_watch.items():
        last = d.get("last_update")
        if not last:
            expired.append(symbol)
            continue

        if (now - last).total_seconds() > expire_seconds:
            expired.append(symbol)

    for symbol in expired:
        global_data.tosama_watch.pop(symbol, None)
