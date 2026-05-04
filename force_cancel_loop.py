# ============================================================
# force_cancel_loop.py（BUY_SELL 準拠・安全最終版）
# ------------------------------------------------------------
# ・30秒ごとに kabusapi/orders を直接確認
# ・未約定の指値注文を全キャンセル
# ・global_data / pending_entries に依存しない
# ・401 / timeout / 切断耐性あり
# ============================================================

import time
import logging
import requests
import configparser
from requests.exceptions import ReadTimeout, ConnectionError, HTTPError

from kabu_api.api_common import get_headers

logger = logging.getLogger(__name__)

BASE_URL = "http://localhost:18080/kabusapi"

# ------------------------------------------------------------
# settings.ini
# ------------------------------------------------------------
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
PASSWORD = conf.get("aukabu", "password", fallback="")

# ------------------------------------------------------------
# Cancel 可能 State
# kabuS API:
# 1=Received, 2=Accepted, 3=Working, 4=PartiallyContracted
# ------------------------------------------------------------
CANCELABLE_STATES = {1, 2, 3, 4}


# ============================================================
# 注文キャンセル
# ============================================================

def cancel_order(order_id):
    payload = {
        "OrderId": order_id,
        "Password": PASSWORD,
    }

    try:
        r = requests.put(
            f"{BASE_URL}/cancelorder",
            headers=get_headers(),
            json=payload,
            timeout=(2, 5),
        )
        r.raise_for_status()

        logger.warning(
            f"[FORCE_CANCEL] order_id={order_id} "
            f"status={r.status_code} body={r.text}"
        )

    except HTTPError as e:
        logger.error(
            f"[FORCE_CANCEL] HTTP error order_id={order_id} "
            f"status={e.response.status_code if e.response else 'N/A'}"
        )

    except Exception:
        logger.exception(f"[FORCE_CANCEL] unexpected error order_id={order_id}")


# ============================================================
# kabu API : 注文取得
# ============================================================

def get_orders():
    try:
        r = requests.get(
            f"{BASE_URL}/orders",
            headers=get_headers(),          # ★ 認証必須
            timeout=(2, 5),
        )
        r.raise_for_status()

        data = r.json()

        # kabu API は dict / list 両方あり得る
        if isinstance(data, dict):
            return data.get("Orders", [])
        if isinstance(data, list):
            return data

        return []

    except HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            logger.error("❌ kabu API Unauthorized (401) in get_orders")
        else:
            logger.exception("❌ kabu API HTTP error in get_orders")
        return []

    except ReadTimeout:
        logger.warning("⚠ kabu API read timeout (get_orders)")
        return []

    except ConnectionError:
        logger.warning("⚠ kabu API connection error (get_orders)")
        return []

    except Exception:
        logger.exception("❌ unexpected error in get_orders")
        return []


# ============================================================
# 強制キャンセルループ
# ============================================================

def start_force_cancel_loop(interval_sec=30):

    logger.warning("🛑 FORCE CANCEL LOOP START (%ss)", interval_sec)

    while True:
        try:
            orders = get_orders()
            if not orders:
                time.sleep(interval_sec)
                continue

            for o in orders:
                order_id = o.get("OrderId") or o.get("ID")
                state = o.get("State")
                price = o.get("Price")
                cum = o.get("CumQty", 0)
                qty = o.get("OrderQty", 0)

                if not order_id:
                    continue

                # 指値 & 未約定 & Cancel可能
                is_limit = price not in (0, None)
                is_open = qty and cum < qty
                can_cancel = state in CANCELABLE_STATES

                if is_limit and is_open and can_cancel:
                    logger.warning(
                        f"[FORCE_CANCEL] CANCEL "
                        f"order_id={order_id} state={state} "
                        f"{cum}/{qty}"
                    )
                    cancel_order(order_id)
                    time.sleep(0.3)  # API 連打防止

        except Exception:
            logger.exception("[FORCE_CANCEL LOOP ERROR]")

        time.sleep(interval_sec)
