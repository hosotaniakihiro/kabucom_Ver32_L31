# websocket_handlers/ws_subscribe.py
import requests
from global_state import global_data

API_URL = "http://localhost:18080/kabusapi"


def register_symbol(symbol: str):
    """
    kabuステ API RegisterSymbol を実行 → PUSH購読を開始
    """
    token = global_data.token_value
    if not token:
        print("❌ token 未設定（register_symbol不可）")
        return False

    url = f"{API_URL}/register"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    payload = {
        "Symbols": [
            {"Symbol": symbol, "Exchange": 1}  # 東証1部（統合市場では1でOK）
        ],
        "SendSummary": False
    }

    res = requests.put(url, json=payload, headers=headers)
    if res.status_code != 200:
        print(f"❌ RegisterSymbol 失敗: {res.text}")
        return False

    return True
