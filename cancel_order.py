# trading/handlers/pending_monitor.py

import time
import logging
from kabu_api.cancel_order import cancel_order
from global_state import global_data

logger = logging.getLogger(__name__)

CANCEL_TIMEOUT_SEC = 15  # ← ここ重要（15秒推奨）

def monitor_pending_orders():
    logger.info("🟢 pending_monitor START (CANCEL ENABLED)")

    while True:
        try:
            now = time.time()

            for order_id, info in list(global_data.pending_orders.items()):
                order_time = info.get("timestamp")
                symbol = info.get("symbol")

                if not order_time:
                    continue

                elapsed = now - order_time

                # ================================
                # 🔥 キャンセル条件
                # ================================
                if elapsed >= CANCEL_TIMEOUT_SEC:
                    logger.warning(
                        f"⏰ PENDING TIMEOUT → CANCEL {symbol} {order_id} ({elapsed:.1f}s)"
                    )

                    try:
                        cancel_order(order_id)
                        logger.info(f"🧹 CANCEL SENT: {order_id}")
                    except Exception as e:
                        logger.error(f"❌ CANCEL FAILED {order_id}: {e}")

                    # 🔥 ここが無いと無限 pending
                    global_data.pending_orders.pop(order_id, None)

        except Exception as e:
            logger.exception(f"pending_monitor error: {e}")

        time.sleep(1)
