# ============================================================
# websocket_handlers/ws_subscribe.py（新規作成）
# ------------------------------------------------------------
# ✔ 銘柄の PUSH 購読 / 解除
# ✔ ランキングENTRYや ENTRY直後の監視に必須
# ============================================================

import requests
import logging
from global_state import global_data

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"


# ------------------------------------------------------------
# 🔹 PUSH購読（register）
# ------------------------------------------------------------
def register_symbol(symbol: str) -> bool:
    token = global_data.token_value
    if not token:
        logger.error("❌ token が未設定 → register_symbol() 失敗")
        return False

    url = f"{API_URL}/register"
    payload = {
        "Symbols": [
            {
                "Symbol": str(symbol),
                "Exchange": 1,  # 東証
            }
        ]
    }

    try:
        res = requests.put(
            url,
            json=payload,
            headers={"X-API-KEY": token, "Content-Type": "application/json"},
            timeout=3,
        )

        if res.status_code == 200:
            logger.info(f"📡 PUSH購読 登録成功: {symbol}")
            return True
        else:
            logger.error(f"❌ register_symbol error: {res.text}")
            return False

    except Exception as e:
        logger.error(f"❌ register_symbol exception: {e}", exc_info=True)
        return False


# ------------------------------------------------------------
# 🔹 PUSH購読解除（unregister）
# ------------------------------------------------------------
def unregister_symbol(symbol: str) -> bool:
    token = global_data.token_value
    if not token:
        logger.error("❌ token 未設定 → unregister_symbol() 失敗")
        return False

    url = f"{API_URL}/unregister"
    payload = {
        "Symbols": [
            {
                "Symbol": str(symbol),
                "Exchange": 1,
            }
        ]
    }

    try:
        res = requests.put(
            url,
            json=payload,
            headers={"X-API-KEY": token, "Content-Type": "application/json"},
            timeout=3,
        )

        if res.status_code == 200:
            logger.info(f"🔕 PUSH購読 解除成功: {symbol}")
            return True
        else:
            logger.error(f"❌ unregister_symbol error: {res.text}")
            return False

    except Exception as e:
        logger.error(f"❌ unregister_symbol exception: {e}", exc_info=True)
        return False
