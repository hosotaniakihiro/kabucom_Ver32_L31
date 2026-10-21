# ============================================================
# trading/handlers/pending_monitor.py
# Ver25-FINAL-PENDING-ORDERS-ONLY
# ------------------------------------------------------------
# ✔ pending_orders のみ監視
# ✔ pending_entries には一切触れない
# ✔ order_id 前提の完全設計
# ✔ スレッド絶対に落ちない
#
# 2026-09-01:
# ✔ pending_orders が空なら /orders を叩かない (無駄な 5 秒 GET を消す)
# ✔ /orders は kabu_api.orders_read_cache 経由で force_cancel_loop と共有
#   (TTL + singleflight + 失敗時バックオフ)
# ✔ cancel 送信 (PUT) 経路はそのまま。キャッシュだけ捨てて次回取り直す
# ============================================================

import time
import datetime as dt
import logging
import requests
import threading

from global_state import global_data
from kabu_api import orders_read_cache
from kabu_api.api_common import get_headers
from kabu_api.cancel_order import cancel_order
from trading.handlers.entry_handler import _unlock_entry

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 5
MAX_PENDING_SEC = 15

CANCELABLE_STATES_NUM = {1, 2, 3, 4}
CANCELABLE_STATES_STR = {
    "Received", "Accepted", "Working", "PartiallyContracted"
}


# ============================================================
# 注文一覧取得
# ============================================================
def _fetch_orders_once():
    """実際の GET。(orders, ok, error) を返す。

    ok=False は「注文が無い」ではなく「読めていない」。
    orders_read_cache がこれを見てバックオフする。
    """
    try:
        r = requests.get(
            "http://localhost:18080/kabusapi/orders",
            headers=get_headers(),
            timeout=5
        )
        if r.status_code != 200:
            logger.error(f"/orders error {r.status_code} {r.text}")
            return [], False, f"status={r.status_code}"
        data = r.json()
        return (data if isinstance(data, list) else []), True, ""
    except Exception as e:
        logger.error(f"/orders request error: {e}")
        return [], False, f"{type(e).__name__}: {e}"


def get_orders():
    """互換 API。読めなかった場合は従来どおり [] を返す。

    実 GET は kabu_api.orders_read_cache が 1 本化する。
    """
    res = orders_read_cache.get_orders_cached(_fetch_orders_once, caller="pending_monitor")
    return list(res.orders) if res.ok else []


def get_orders_state():
    """orders と「読めたかどうか」を両方返す。0 件と読めない を区別する。"""
    return orders_read_cache.get_orders_cached(_fetch_orders_once, caller="pending_monitor")


# ============================================================
# pending monitor 起動
# ============================================================
def start_pending_monitor():

    def loop():
        logger.info("🟢 pending_monitor START (pending_orders ONLY)")

        while True:
            try:
                now = dt.datetime.now()

                # =================================================
                # ★ pending_orders のみ監視
                #   監視対象が無いときに /orders を叩かない。
                #   (旧実装は毎 5 秒 GET していて、kabu Station が
                #    無応答のときはそのまま 5 秒 timeout を踏み続けた)
                # =================================================
                pending_orders = getattr(global_data, "pending_orders", None)
                if not isinstance(pending_orders, dict) or not pending_orders:
                    time.sleep(CHECK_INTERVAL)
                    continue

                res = get_orders_state()
                if not res.ok:
                    # 読めていない。0 件と混同してキャンセル判断をしない。
                    logger.warning(
                        "[PENDING] /orders を読めていないためこの周回は判定しない "
                        "source=%s err=%s pending=%d",
                        res.source, str(res.error)[:120], len(pending_orders),
                    )
                    time.sleep(CHECK_INTERVAL)
                    continue

                order_map = {
                    (o.get("OrderId") or o.get("ID")): o
                    for o in res.orders
                    if isinstance(o, dict)
                }

                for order_id, info in list(pending_orders.items()):
                    try:
                        if not isinstance(info, dict):
                            pending_orders.pop(order_id, None)
                            continue

                        symbol = info.get("symbol")
                        ts = info.get("timestamp")

                        if not symbol or not isinstance(ts, dt.datetime):
                            pending_orders.pop(order_id, None)
                            continue

                        elapsed = (now - ts).total_seconds()

                        o = order_map.get(order_id)
                        if not o:
                            continue

                        state = o.get("State")
                        price = o.get("Price")
                        cum = o.get("CumQty", 0)
                        qty = o.get("OrderQty", 0)

                        logger.info(
                            f"[PENDING] {symbol} {order_id} "
                            f"state={state} {cum}/{qty} {elapsed:.1f}s"
                        )

                        # =============================
                        # 約定完了
                        # =============================
                        if state in ("Contracted", 5):
                            pending_orders.pop(order_id, None)
                            _unlock_entry(symbol)
                            continue

                        # =============================
                        # Cancel 完了
                        # =============================
                        if state in ("Canceled", 6):
                            pending_orders.pop(order_id, None)
                            _unlock_entry(symbol)
                            continue

                        # =============================
                        # 未約定 → Cancel
                        # =============================
                        if (
                            elapsed >= MAX_PENDING_SEC
                            and price not in (0, None)
                            and (
                                state in CANCELABLE_STATES_NUM
                                or state in CANCELABLE_STATES_STR
                            )
                        ):
                            logger.warning(f"⚠ Cancel送信 {symbol} {order_id}")
                            # 取消 (PUT) はキャッシュ/バックオフの対象外。
                            # 送った直後の注文状態は必ず取り直す。
                            cancel_order(order_id)
                            orders_read_cache.invalidate(reason="pending_monitor.cancel")

                    except Exception:
                        logger.exception("[PENDING_ITEM_ERROR]")

                time.sleep(CHECK_INTERVAL)

            except Exception:
                logger.exception("[PENDING_LOOP_FATAL]")
                time.sleep(CHECK_INTERVAL)

    threading.Thread(target=loop, daemon=True).start()
