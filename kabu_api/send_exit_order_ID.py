# kabu_api/send_exit_order.py
import requests
import logging
from token_manager import get_valid_token
from configparser import ConfigParser

conf = ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password")
API_URL = "http://localhost:18080/kabusapi"

logger = logging.getLogger(__name__)


def send_exit_order(symbol, hold_id, qty, side, price=0):
    """
    kabu API の信用返済専用の正しいEXIT注文
    BUY_CREDIT → 売り返済 (Side=1)
    SELL_CREDIT → 買い戻し (Side=2)
    """
    token = get_valid_token()

    # 返済方向
    if side == "BUY_CREDIT":
        exit_side = 1   # 売り返済
    else:
        exit_side = 2   # 買い戻し

    payload = {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": 1,
        "SecurityType": 1,
        "Side": exit_side,
        "CashMargin": 3,
        "MarginTradeType": 1,     # ★★★ 返済！！これが最重要 ★★★
        "DelivType": 2,
        "AccountType": 4,
        "Qty": int(qty),
        "FrontOrderType": 10,     # 成行
        "Price": 0,
        "ExpireDay": 0,
        "ClosePositions": [
            {
                "HoldID": hold_id,
                "Qty": int(qty)
            }
        ]
    }

    logger.info(f"📤 EXIT送信: symbol={symbol}, hold_id={hold_id}")
    logger.debug(f"EXIT Payload: {payload}")

    url = f"{API_URL}/sendorder"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=5)

        if res.status_code == 200:
            data = res.json()
            logger.info(f"✅ EXIT成功: OrderID={data.get('OrderId')}")
            return data.get("OrderId")

        logger.error(f"❌ EXIT失敗: {res.status_code} {res.text}")
        return None

    except Exception as e:
        logger.error(f"❌ send_exit_order エラー: {e}", exc_info=True)
        return None
