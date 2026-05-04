# kabu_api/cancel_order.py
import requests
from kabu_api.utils import API_URL
from token_manager import get_valid_token

def cancel_order(order_id: str, token: str = None):
    """
    kabu API 注文取消し
    """
    if not token:
        token = get_valid_token()
    url = f"{API_URL}/cancelorder"
    headers = {"X-API-KEY": token}
    body = {"OrderId": order_id}
    res = requests.put(url, headers=headers, json=body)
    if res.status_code == 200:
        return True
    else:
        print(f"❌ cancel_order 失敗: {res.text}")
        return False
