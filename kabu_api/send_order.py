# ============================================================
# kabu_api/send_order.py（Ver23-FINAL-FULL-FIX）
# ------------------------------------------------------------
# ・成功時は dict {"OrderId": "...", "Price": <float>} を返す
# ・失敗時は None
# ・buy_sell_entry と entry_handler が完全に動作する形に統一
# ・レスポンスが文字列にならないように統一（最重要）
# ============================================================

import requests
import json
import logging
import configparser
from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")


# ============================================================
# 🌐 統一注文 API（常に dict を返す）
# ============================================================
def send_order_common(payload: dict):
    """
    kabuステーションAPI /sendorder を呼ぶ共通関数。

    戻り値（成功時）:
        { "OrderId": "...", "Price": float }

    戻り値（失敗時）:
        None
    """

    token = get_valid_token()
    if not token:
        logger.error("❌ send_order_common: APIトークン取得失敗")
        return None

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token
    }

    url = f"{API_URL}/sendorder"

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=5)

        # -------------------------------------------------------
        # HTTPエラー処理（JSONを取り出してログに表示）
        # -------------------------------------------------------
        if res.status_code != 200:
            try:
                data = res.json()
            except Exception:
                data = res.text
            logger.error(f"❌ HTTPエラー {res.status_code}: {data}")
            return None

        # -------------------------------------------------------
        # レスポンスJSON
        # -------------------------------------------------------
        try:
            data = res.json()
        except Exception:
            logger.error("❌ send_order_common: API JSON 解析失敗")
            return None

        order_id = data.get("OrderId")
        if not order_id:
            logger.error(f"❌ API応答異常（OrderIdなし）: {data}")
            return None

        # kabuS API は約定価格を返さないため payload.Price を返す
        executed_price = float(payload.get("Price", 0))

        logger.info(f"🟢 send_order_common 成功: OrderId={order_id}")

        # ★★★ 最重要：dict で返す（文字列だけ返さない！）★★★
        return {
            "OrderId": order_id,
            "Price": executed_price
        }

    except Exception as e:
        logger.error(f"❌ send_order_common 例外: {e}", exc_info=True)
        return None
