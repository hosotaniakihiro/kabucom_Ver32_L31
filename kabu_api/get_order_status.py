#kabu_api/get_order_status.py

import requests
from kabu_api.utils import API_URL
from token_manager import get_valid_token

def get_order_status(order_id: str, token: str = None):
    """
    注文状態を取得する (kabuステーションAPI)
    - order_id: 注文ID
    - 戻り値: dict (注文情報) または None
    """
    if not token:
        token = get_valid_token()

    url = f"{API_URL}/orders"
    headers = {"X-API-KEY": token}

    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        orders = res.json()

        # 注文IDでフィルタ
        for order in orders:
            if order.get("ID") == order_id:
                return order
        return None
    except Exception as e:
        print(f"❌ get_order_status エラー: {e}")
        return None
