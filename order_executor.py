# order_executor.py

import json
import requests
import traceback
import logging
import pprint # pprintを追加

# グローバルな状態管理クラスからトークンを取得するように変更
from global_state import global_data as gd

logger = logging.getLogger(__name__)

def execute_buy_order(symbol, price, qty):
    """
    信用新規買い注文を送信します（指値）。
    """
    token = gd.token_value # グローバルデータからトークンを取得
    if not token:
        logger.error("❌ トークンが設定されていません。買い注文をキャンセルします。")
        return None

    print(f"🟢 BUY ORDER: {symbol} {qty}株 @ {price:.2f}円")

    url = "http://localhost:18080/kabusapi/sendorder"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token
    }

    body = {
        'Password': 'YOUR_API_PASSWORD', # パスワードは設定ファイルから読み込む必要があります
        'Symbol': symbol,
        'Side': '2',  # 買い
        'CashMargin': 2,
        'MarginTradeType': 1,
        'DelivType': 0,
        'Qty': qty,
        'FrontOrderType': 20,  # 指値
        'Price': price,
        'ExpireDay': 0,
        'Exchange': 1,
        'SecurityType': 1,
        'AccountType': 4
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(body))
        response.raise_for_status()
        content = response.json()
        print(f"✅ 注文成功: OrderId = {content.get('OrderId')}")
        return content.get('OrderId')
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ 新規買いHTTPエラー: {e}")
        try:
            content = e.response.json()
            pprint.pprint(content)
        except json.JSONDecodeError:
            logger.error("レスポンスのデコードに失敗しました。")
        return None
    except Exception as e:
        logger.error(f"❌ 新規買いその他エラー: {e}")
        traceback.print_exc()
        return None


def execute_sell_order(symbol, qty, hold_id, price=0):
    """
    信用返済売り注文を送信します。
    - price = 0 の場合 → 成行（FrontOrderType=10）
    - price > 0 の場合 → 指値（FrontOrderType=20）
    """
    token = gd.token_value
    if not token:
        logger.error("❌ トークンが設定されていません。売り注文をキャンセルします。")
        return None

    order_type = 10 if price == 0 else 20  # 成行 or 指値
    price_str = "成行" if price == 0 else f"指値 {price}"

    print(f"🔴 SELL ORDER ({price_str}): {symbol} {qty}株")

    url = "http://localhost:18080/kabusapi/sendorder"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token
    }

    body = {
        "Password": "YOUR_API_PASSWORD",  # ← settings.iniから読むよう修正推奨
        "Symbol": symbol,
        "Exchange": 1,
        "SecurityType": 1,
        "Side": "1",        # 売り
        "CashMargin": 3,    # 信用返済
        "DelivType": 0,
        "AccountType": 4,
        "Qty": qty,
        "FrontOrderType": order_type,
        "Price": price,
        "ExpireDay": 0,
        "ClosePositions": [
            {
                "HoldID": hold_id,
                "Qty": qty
            }
        ]
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(body))
        response.raise_for_status()
        content = response.json()
        print(f"✅ 返済売り注文成功 ({price_str}): OrderId = {content.get('OrderId')}")
        return content.get("OrderId")
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ 売り注文HTTPエラー: {e}")
        try:
            content = e.response.json()
            pprint.pprint(content)
        except json.JSONDecodeError:
            logger.error("レスポンスのデコードに失敗しました。")
        return None
    except Exception as e:
        logger.error(f"❌ 売り注文その他エラー: {e}")
        traceback.print_exc()
        return None


def is_order_filled(order_id):
    """
    注文IDを指定して約定したか確認します。
    """
    token = gd.token_value  # グローバルデータからトークンを取得
    if not token:
        logger.error("❌ トークンが設定されていません。約定確認をスキップします。")
        return False

    url = 'http://localhost:18080/kabusapi/orders'
    headers = {
        'Content-Type': 'application/json',
        'X-API-KEY': token
    }

    try:
        response = requests.get(f'{url}?product=0', headers=headers)
        response.raise_for_status()
        orders = response.json()
        for order in orders:
            if order["OrderId"] == order_id:
                for detail in order.get("Details", []):
                    # State=5 は約定済みを示す
                    if detail.get("State") == 5:
                        logger.info(f"✅ 約定確認済: OrderId = {order_id}")
                        return True
        logger.info(f"⏳ 未約定: OrderId = {order_id}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 約定確認エラー: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ 約定確認その他エラー: {e}")
        traceback.print_exc()
        return False

