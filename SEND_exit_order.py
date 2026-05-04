# send_exit_order.py（プロジェクト直下用 単体実行版）

import requests
import logging
import os
from configparser import ConfigParser
from token_manager import get_valid_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# settings.ini（プロジェクト直下）の絶対パスを取得
# ------------------------------------------------------------
BASE_DIR = os.path.dirname(__file__)   # ← プロジェクト直下
INI_PATH = os.path.join(BASE_DIR, "settings.ini")

conf = ConfigParser()
conf.read(INI_PATH, encoding="utf-8")

Password = conf.get("aukabu", "password")
API_URL = "http://localhost:18080/kabusapi"


def send_exit_order():
    """
    単体実行向け EXIT 注文
    """

    # ------------------------------------------
    # 実行パラメータ
    # ------------------------------------------
    symbol = 1813


    qty = 200
    side = "BUY_CREDIT"
    #side = "SELL_CREDIT"
    #hold_id = "TEST"
    # ------------------------------------------

    token = get_valid_token()

    # EXIT 方向決定
    if side == "BUY_CREDIT":
        exit_side = 1  # 売り返済
    elif side == "SELL_CREDIT":
        exit_side = 2  # 買い戻し
    else:
        print("❌ side が不正:", side)
        return

    payload = {
        "Password": Password,
        "Symbol": symbol,
        "Exchange": 1,
        "SecurityType": 1,
        "Side": exit_side,
        "CashMargin": 3,
        "MarginTradeType": 1,   # 信用返済
        "DelivType": 2,
        "AccountType": 4,
        "Qty": qty,
        "FrontOrderType": 10,   # 10:成行  　　　　20；指値
        "Price": 0,
        "ExpireDay": 0,
        "ClosePositionOrder": 1
    }

    print(f"📤 EXIT送信: {payload}")

    url = f"{API_URL}/sendorder"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token
    }

    res = requests.post(url, json=payload, headers=headers)

    print("HTTP Status:", res.status_code)
    print("Response:", res.text)


# ------------------------------------------------------------
# 単体実行
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=== EXIT ORDER TEST ===")
    send_exit_order()
