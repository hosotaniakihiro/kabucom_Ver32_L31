# ============================================================
# kabu_api/send_entry_order.py（Ver17.1 指値ENTRY API）
# ============================================================

import requests
import logging
import json
import configparser

from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")

Password = conf.get("aukabu", "password", fallback="")


def send_entry_order(symbol: str, side: str, qty: int, price: float) -> str | None:
    """
    指値で信用新規発注する。
      - price: 指値（trigger_price をそのまま渡す）
      - side: BUY_CREDIT / SELL_CREDIT
    """
    token = get_valid_token()
    if not token:
        logger.error("❌ トークン取得失敗")
        return None

    api_side = 1 if side == "BUY_CREDIT" else 2  # kabuステ仕様

    payload = {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": 1,
        "SecurityType": 1,
        "Side": api_side,
        "CashMargin": 2,           # 信用新規
        "DelivType": 2,
        "AccountType": 4,
        "Qty": int(qty),
        "FrontOrderType": 20,      # 🔥 指値
        "Price": int(price),       # 🔥 trigger_price → 指値
        "ExpireDay": 0,
    }

    try:
        url = f"{API_URL}/sendorder"
        headers = {"Content-Type": "application/json", "X-API-KEY": token}

        res = requests.post(url, headers=headers, data=json.dumps(payload))
        data = res.json()

        if res.status_code != 200:
            logger.error(f"❌ send_entry_order HTTP {res.status_code}: {data}")
            return None

        oid = data.get("OrderId")
        if not oid:
            logger.error(f"❌ send_entry_order 応答異常: {data}")
            return None

        logger.info(
            f"🟢 ENTRY（指値）成功: {symbol} side={side} qty={qty} price={price} order_id={oid}"
        )
        return oid

    except Exception as e:
        logger.error(f"❌ send_entry_order({symbol}) エラー: {e}", exc_info=True)
        return None
