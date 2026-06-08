# kabu_api/api_common.py
import logging
import os
import time

import requests
from token_manager import get_valid_token
from global_state import global_data

API_URL = "http://localhost:18080/kabusapi"
logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _set_global_token(token: str) -> None:
    """global_data の複数互換属性へ token を同期する。"""
    if not token:
        return
    for name in ("token_value", "API_TOKEN", "api_token", "token"):
        try:
            setattr(global_data, name, token)
        except Exception:
            pass


def _read_global_token() -> str | None:
    for name in ("token_value", "API_TOKEN", "api_token", "token"):
        try:
            token = getattr(global_data, name, None)
            if token:
                return str(token)
        except Exception:
            pass
    return None


def _read_token_manager() -> str | None:
    try:
        token = get_valid_token()
    except Exception:
        logger.exception("[API COMMON] get_valid_token failed")
        token = None
    if token:
        token = str(token)
        _set_global_token(token)
        logger.warning("[API COMMON] token restored from token_manager into global_data")
        return token
    return None


def _get_api_token() -> str:
    """
    kabuステ API token を安全に取得する。

    旧実装は global_data.token_value だけを見ていたため、
    token_manager 側に token が存在しても force_cancel_loop / entry / exit 側で
    `API TOKEN is not set in global_data` になっていた。

    V2:
      起動直後は token_manager 初期化より force_cancel/entry が先に動くことがある。
      短時間だけ待ってから失敗することで、候補生成直後の発注取りこぼしを減らす。
    """
    token = _read_global_token()
    if token:
        return token

    token = _read_token_manager()
    if token:
        return token

    wait_sec = max(0.0, _env_float("KABU_API_TOKEN_WAIT_SEC", 5.0))
    poll_sec = max(0.1, _env_float("KABU_API_TOKEN_WAIT_POLL_SEC", 0.25))
    deadline = time.perf_counter() + wait_sec
    warned = False
    while time.perf_counter() < deadline:
        if not warned:
            logger.warning("[API COMMON] API TOKEN not ready; wait up to %.1fs", wait_sec)
            warned = True
        time.sleep(poll_sec)
        token = _read_global_token()
        if token:
            logger.warning("[API COMMON] token appeared in global_data after wait")
            return token
        token = _read_token_manager()
        if token:
            return token

    raise RuntimeError("API TOKEN is not set in global_data/token_manager")


def get_trading_unit(symbol: str, exchange: int = 1) -> int:
    """
    銘柄ごとの単元株数を /board から取得
    """
    token = _get_api_token()
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
    token = _get_api_token()
    url = f"{API_URL}/wallet/margin"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        data = res.json()
        return float(data.get("MarginAvailable", 0))
    except Exception:
        return 0.0


def calculate_shares(price: float, budget: float = 700000, unit_size: int = 100) -> int:
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
    token = _get_api_token()
    return {
        "X-API-KEY": token,
        "Content-Type": "application/json",
    }
