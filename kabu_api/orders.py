# kabu_api/orders.py（Ver18-FINAL）
import requests
from kabu_api.utils import API_URL, get_valid_token
import logging

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# 🟦 注文一覧を取得（当日分）
# ------------------------------------------------------------
def get_all_orders():
    """
    kabuステーションAPI /orders
    当日注文をすべて取得（未約定・約定済み含む）
    """
    try:
        token = get_valid_token()
        url = f"{API_URL}/orders"
        headers = {"X-API-KEY": token}

        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()

        return res.json()

    except Exception as e:
        logger.error(f"❌ get_all_orders エラー: {e}")
        return []


# ------------------------------------------------------------
# 🟦 個別注文情報
# ------------------------------------------------------------
def get_order_info(order_id):
    try:
        token = get_valid_token()
        url = f"{API_URL}/orders/{order_id}"
        headers = {"X-API-KEY": token}

        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()

        return res.json()

    except Exception as e:
        logger.error(f"❌ get_order_info エラー: {order_id}: {e}")
        return None


# ------------------------------------------------------------
# 🟥 注文キャンセル
# ------------------------------------------------------------
def cancel_order(order_id):
    try:
        token = get_valid_token()
        url = f"{API_URL}/orders/{order_id}"
        headers = {"X-API-KEY": token}

        res = requests.delete(url, headers=headers, timeout=5)

        if res.status_code in (200, 409):
            logger.info(f"🟡 キャンセル成功: {order_id}")
            return True

        logger.error(f"❌ cancel_order 失敗[{res.status_code}]: {res.text}")
        return False

    except Exception as e:
        logger.error(f"❌ cancel_order エラー: {order_id} {e}")
        return False
# ============================================================
# compatibility function
# ============================================================

def check_order_status(*args, **kwargs):
    """
    旧コード互換
    現在の注文状態チェックが無い場合のダミー
    """
    return None