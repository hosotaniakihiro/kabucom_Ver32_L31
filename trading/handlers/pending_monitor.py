# ============================================================
# trading/handlers/pending_monitor.py
# Ver25-FINAL-PENDING-ORDERS-ONLY
# ------------------------------------------------------------
# ✔ pending_orders のみ監視
# ✔ pending_entries には一切触れない
# ✔ order_id 前提の完全設計
# ✔ スレッド絶対に落ちない
# ============================================================

import time
import datetime as dt
import logging
import requests
import threading

from global_state import global_data
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
def get_orders():
    try:
        r = requests.get(
            "http://localhost:18080/kabusapi/orders",
            headers=get_headers(),
            timeout=5
        )
        if r.status_code != 200:
            logger.error(f"/orders error {r.status_code} {r.text}")
            return []
        return r.json()
    except Exception as e:
        logger.error(f"/orders request error: {e}")
        return []


# ============================================================
# pending monitor 起動
# ============================================================
def start_pending_monitor():

    def loop():
        logger.info("🟢 pending_monitor START (pending_orders ONLY)")

        while True:
            try:
                now = dt.datetime.now()
                orders = get_orders()

                order_map = {
                    (o.get("OrderId") or o.get("ID")): o
                    for o in orders
                    if isinstance(o, dict)
                }

                # =================================================
                # ★ pending_orders のみ監視
                # =================================================
                pending_orders = getattr(global_data, "pending_orders", None)
                if not isinstance(pending_orders, dict) or not pending_orders:
                    time.sleep(CHECK_INTERVAL)
                    continue

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
                            cancel_order(order_id)

                    except Exception:
                        logger.exception("[PENDING_ITEM_ERROR]")

                time.sleep(CHECK_INTERVAL)

            except Exception:
                logger.exception("[PENDING_LOOP_FATAL]")
                time.sleep(CHECK_INTERVAL)

    threading.Thread(target=loop, daemon=True).start()
