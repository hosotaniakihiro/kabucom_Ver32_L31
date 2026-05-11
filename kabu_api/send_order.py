# ============================================================
# kabu_api/send_order.py（Ver23.1-FINAL-100368-REJECT-CACHE）
# ------------------------------------------------------------
# ・成功時は dict {"OrderId": "...", "Price": <float>} を返す
# ・失敗時は None
# ・buy_sell_entry と entry_handler が完全に動作する形に統一
# ・レスポンスが文字列にならないように統一（最重要）
# ・Code=100368 を検出したら SELL 拒否キャッシュへ登録
# ============================================================

import requests
import logging
import configparser
from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")


def _is_sell_order_payload(payload: dict) -> bool:
    try:
        # kabu API: Side=1 が売り。CashMargin=2 が新規信用。
        return int(payload.get("Side", 0)) == 1 and int(payload.get("CashMargin", 0)) == 2
    except Exception:
        return False


def _mark_sell_reject_if_needed(payload: dict, data) -> None:
    try:
        if not _is_sell_order_payload(payload):
            return

        code = ""
        message = ""
        if isinstance(data, dict):
            code = str(data.get("Code") or data.get("code") or "").strip()
            message = str(data.get("Message") or data.get("message") or "")
        else:
            message = str(data or "")

        if code != "100368" and not ("信用新規" in message and "抑止" in message):
            return

        symbol = str(payload.get("Symbol") or "").strip()
        if not symbol:
            return

        from AI.sell_order_reject_cache import mark_sell_rejected

        mark_sell_rejected(
            symbol,
            code=code or "100368",
            message=message,
            source="kabu_api.send_order_common",
        )
    except Exception:
        logger.exception("[SEND ORDER] failed to mark sell reject cache payload=%s data=%s", payload, data)


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

            _mark_sell_reject_if_needed(payload, data)

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
            _mark_sell_reject_if_needed(payload, data)
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
