# kabu_api/get_current_price.py
# kabu_api/get_current_price.py
import logging
import time
import requests
import pandas as pd
from token_manager import get_valid_token
from global_state import global_data

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

# ===== キャッシュ用 =====
_price_cache: dict[str, float] = {}
_last_call_time: dict[str, float] = {}


def get_price_from_push(symbol: str) -> float | None:
    """
    PUSHデータ（global_data.global_dataframe）から最新価格を取得する
    """
    try:
        df = global_data.global_dataframe
        if df is None or df.empty:
            return None

        df_symbol = df[df["symbol"].astype(str) == str(symbol)]
        if df_symbol.empty:
            return None

        latest = df_symbol.sort_values("time").iloc[-1]
        price = latest.get("price") or latest.get("currentprice") or latest.get("CurrentPrice")
        return float(price) if price is not None else None
    except Exception as e:
        logger.warning(f"⚠️ pushデータから価格取得失敗: {symbol} {e}")
        return None


def get_price_from_board(symbol: str, exchange: int = 1, token: str = None) -> float | None:
    """
    /board APIから価格を取得（必要なときだけ利用）
    """
    if not token:
        token = get_valid_token()
    if not token:
        logger.error("❌ トークン未取得のため /board 呼び出し不可")
        return None

    url = f"{API_URL}/board/{symbol}@{exchange}"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()
        data = res.json()
        price = data.get("CurrentPrice")
        return float(price) if price is not None else None
    except requests.exceptions.HTTPError as e:
        try:
            logger.error(f"❌ /board HTTPエラー: {symbol} {e.response.json()}")
        except Exception:
            logger.error(f"❌ /board HTTPエラー: {symbol} {e}")
        return None
    except Exception as e:
        logger.error(f"❌ get_price_from_board 例外: {symbol} {e}")
        return None


def get_price_with_fallback(symbol: str, token: str = None) -> float | None:
    """
    東証→PTS の順に /board で取得する（将来用）
    """
    price = get_price_from_board(symbol, exchange=1, token=token)
    if price is None:
        price = get_price_from_board(symbol, exchange=5, token=token)
    return price


def get_current_price(symbol: str, token=None, use_api=True):
    # ✅ token が DataFrame の場合も検出して None 扱いにする
    if token is None or isinstance(token, pd.DataFrame) or (isinstance(token, str) and token.strip() == ""):
        # ここで token を再取得する処理を呼ぶ
        token = get_valid_token()
    symbol = str(symbol)

    # 1️⃣ PUSHデータ優先
    price = get_price_from_push(symbol)
    if price is not None:
        return price

    # APIを使わない設定ならここで終了
    if not use_api:
        return None

    # 2️⃣ /board API呼び出し（2秒キャッシュ付き）
    if not token:
        token = get_valid_token()

    now = time.time()
    if symbol in _last_call_time and now - _last_call_time[symbol] < 2:
        return _price_cache.get(symbol)

    url_board = f"{API_URL}/board/{symbol}@1"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    for attempt in range(2):  # 最大2回
        try:
            res = requests.get(url_board, headers=headers, timeout=10)
            res.raise_for_status()
            data = res.json()
            price = data.get("CurrentPrice")

            _last_call_time[symbol] = now
            _price_cache[symbol] = price

            if price is not None:
                logger.info(f"✅ /boardから価格取得成功: {symbol}={price}")
                return float(price)
            else:
                logger.warning(f"⚠️ /boardから価格取得: 現在値なし {symbol}")
                return None

        except Exception as e:
            logger.warning(f"⚠️ /board API失敗: {symbol} {e} (attempt {attempt+1})")
            if attempt == 0:
                time.sleep(1)
            else:
                return None

    return None


# --- 後方互換エイリアス ---
# 古いコード（monitor_orders.pyなど）用
def get_latest_price(symbol: str, token: str = None) -> float | None:
    """
    旧バージョン互換の価格取得関数
    → PUSHデータ優先で返す
    """
    return get_price_from_push(symbol)
