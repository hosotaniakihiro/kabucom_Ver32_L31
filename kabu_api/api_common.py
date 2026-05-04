# kabu_api/api_common.py
import requests
from token_manager import get_valid_token
from global_state import global_data
API_URL = "http://localhost:18080/kabusapi"


def get_trading_unit(symbol: str, exchange: int = 1) -> int:
    """
    銘柄ごとの単元株数を /board から取得
    """
    token = get_valid_token()
    url = f"{API_URL}/board/{symbol}@{exchange}"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        data = res.json()
        return int(data.get("TradingUnit", 100))
    except Exception:
        return 100


def get_margin_available() -> float:
    """
    信用建余力を /wallet/margin から取得
    """
    token = get_valid_token()
    url = f"{API_URL}/wallet/margin"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        data = res.json()
        return float(data.get("MarginAvailable", 0))
    except Exception:
        return 0.0


def calculate_shares(price: float, budget: float = 500000, unit_size: int = 100) -> int:
    """
    予算と株価から買える株数を計算（unit_size単位で切り捨て）
    """
    if price is None or budget is None:
        return 0
    if price <= 0 or budget <= 0:
        return 0

    max_shares = budget // price
    lots = max_shares // unit_size
    qty = int(lots * unit_size)
    return qty

def get_headers():
    """
    kabuステ API 用 共通ヘッダ
    """
    token = global_data.token_value
    if not token:
        raise RuntimeError("API TOKEN is not set in global_data")

    return {
        "X-API-KEY": token,
        "Content-Type": "application/json"
    }