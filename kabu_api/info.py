import requests
#from kabu_api.utils import API_URL, get_valid_token
from kabu_api.utils import API_URL          # ✅ API_URL は utils から
from token_manager import get_valid_token
def get_positions():
    """信用建玉一覧を取得"""
    token = get_valid_token()
    url = f"{API_URL}/positions"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        return res.json() or []
    except Exception as e:
        print(f"❌ get_positions エラー: {e}")
        return []

def get_best_quotes(symbol):
    """板情報から最良気配を取得"""
    token = get_valid_token()
    url = f"{API_URL}/board/{str(symbol).zfill(4)}"
    headers = {"X-API-KEY": token}
    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        board = res.json()
        return board.get("AskPrice"), board.get("BidPrice")
    except Exception as e:
        print(f"❌ get_best_quotes エラー: {e}")
        return None, None

def get_latest_price(symbol):
    """現在値を取得"""
    token = get_valid_token()
    url = f"{API_URL}/board/{str(symbol).zfill(4)}"
    headers = {"X-API-KEY": token}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.raise_for_status()
        return resp.json().get("CurrentPrice")
    except Exception as e:
        print(f"⚠️ get_latest_price エラー: {e}")
        return None

def fetch_orders():
    """注文一覧を取得"""
    import urllib.request, json
    token = get_valid_token()
    url = f"{API_URL}/orders"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read())
    except Exception as e:
        print(f"❌ fetch_orders エラー: {e}")
        return []

def check_order_status(order_id: str):
    """注文の約定状況を確認"""
    token = get_valid_token()
    url = f"{API_URL}/orders"
    try:
        resp = requests.get(f"{url}?product=0", headers={"X-API-KEY": token})
        resp.raise_for_status()
        for order in resp.json():
            if order.get("OrderId") == order_id:
                total_qty = order.get("Qty", 0)
                details = order.get("Details", [])
                exec_qty = sum(d.get("ExecutionQty", 0) for d in details)
                exec_amt = sum(d.get("ExecutionQty", 0) * d.get("ExecutionPrice", 0) for d in details)
                avg_price = exec_amt / exec_qty if exec_qty > 0 else None
                all_filled = (exec_qty == total_qty)
                return exec_qty, avg_price, total_qty, all_filled
        return 0, None, 0, False
    except Exception as e:
        print(f"❌ check_order_status エラー: {e}")
        return 0, None, 0, False
def is_order_filled(order_id):
    """指定注文が約定済みか確認"""
    token = get_valid_token()
    url = f"{API_URL}/orders"

    try:
        response = requests.get(f"{url}?product=0", headers={"X-API-KEY": token})
        response.raise_for_status()
        orders = response.json()
        for order in orders:
            if order["OrderId"] == order_id:
                for detail in order.get("Details", []):
                    if detail.get("State") == 5:  # 5=約定済
                        print(f"✅ 約定確認済: OrderId = {order_id}")
                        return True
        return False
    except Exception as e:
        print(f"❌ is_order_filled エラー: {e}")
        return False

def get_api_info(token: str = None):
    """
    kabuステーションAPIのバージョン情報などを取得
    """
    try:
        if not token:
            token = get_valid_token()
        if not token:
            logger.error("❌ トークン取得失敗 → API情報取得中止")
            return None

        url = f"{API_URL}/info"
        headers = {"X-API-KEY": token}
        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()

        info = res.json()
        logger.info(f"✅ API情報取得成功: {info.get('ApiVersion', 'N/A')}")
        return info

    except Exception as e:
        logger.error(f"❌ get_api_info エラー: {e}", exc_info=True)
        return None