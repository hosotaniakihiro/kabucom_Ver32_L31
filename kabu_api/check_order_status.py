#kabus_api/check_order_status.py
import requests
import json
from token_manager import get_valid_token
from config_loader import API_URL

def check_order_status(order_id):
    """
    kabuステーションAPIの /orders から注文状況を確認する
    """
    token = get_valid_token()
    url = f"{API_URL}/orders"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        orders = response.json()

        print("DEBUG: /orders レスポンス:", orders)

        if not orders:
            print("⚠️ 注文一覧が空です")
            return None

        # ← OrderId ではなく ID で比較
        for order in orders:
            if order.get("ID") == order_id:
                executed_qty = order.get("CumQty", 0)
                order_qty = order.get("OrderQty", 0)
                avg_price = order.get("Price", None)
                state = order.get("State")

                print(f"✅ 注文 {order_id} を確認しました")
                print(f"株数: {executed_qty}/{order_qty}, 価格: {avg_price}, 状態: {state}")
                return order

        print(f"⚠️ 注文ID {order_id} が見つかりません")
        return None

    except Exception as e:
        print(f"❌ 注文ステータス取得エラー: {e}")
        try:
            print("レスポンス本文:", response.json())
        except Exception:
            pass
        return None
