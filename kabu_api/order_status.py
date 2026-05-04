# ============================================================
# kabu_api/order_status.py
# ============================================================

import requests
import configparser
import json
import logging

from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")

Password = conf.get("aukabu", "password")


# ------------------------------------------------------------
# 🔹 注文状態を取得
# ------------------------------------------------------------
def get_order_status(order_id: str):
    token = get_valid_token()
    if not token:
        return None

    url = f"{API_URL}/orders"
    headers = {"X-API-KEY": token}

    try:
        res = requests.get(url, headers=headers)
        data = res.json()

        for od in data:
            if od.get("OrderId") == order_id:
                return od

        return None

    except Exception as e:
        logger.error(f"❌ get_order_status エラー: {e}", exc_info=True)
        return None


# ------------------------------------------------------------
# 🔹 注文キャンセル
# ------------------------------------------------------------
def cancel_order(order_id: str):
    token = get_valid_token()
    if not token:
        return None

    url = f"{API_URL}/cancelorder"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token,
    }

    payload = {
        "OrderId": order_id,
        "Password": Password,
    }

    try:
        res = requests.post(url, headers=headers, data=json.dumps(payload))
        if res.status_code != 200:
            logger.error(f"❌ 注文キャンセル失敗 {order_id}: {res.text}")
        else:
            logger.info(f"🧹 注文キャンセル成功 {order_id}")

    except Exception as e:
        logger.error(f"❌ cancel_order エラー: {e}", exc_info=True)
