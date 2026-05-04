# kabu_api/http_requests.py
import requests
import logging

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

def call_symbol_api(symbol: str, token: str, exchange: int = 1, timeout: int = 5) -> dict | None:
    """ /symbol APIをクエリ形式で叩く """
    url = f"{API_URL}/symbol?symbol={symbol}&exchange={exchange}"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    try:
        res = requests.get(url, headers=headers, timeout=timeout)
        res.raise_for_status()
        return res.json()
    except requests.exceptions.RequestException as e:
        logger.warning(f"❌ call_symbol_api失敗: {symbol} {e}")
        try:
            logger.warning(f"レスポンス本文: {res.text}")
        except Exception:
            pass
        return None


def call_board_api(symbol: str, token: str, exchange: int = 1, timeout: int = 5) -> dict | None:
    """ /board APIをクエリ形式で叩く """
    url = f"{API_URL}/board?symbol={symbol}&exchange={exchange}"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    try:
        res = requests.get(url, headers=headers, timeout=timeout)
        res.raise_for_status()
        return res.json()
    except requests.exceptions.RequestException as e:
        logger.warning(f"❌ call_board_api失敗: {symbol} {e}")
        try:
            logger.warning(f"レスポンス本文: {res.text}")
        except Exception:
            pass
        return None