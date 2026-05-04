# kabu_api/get_board.py
import logging
import requests
import time
import os
import traceback
from kabu_api.utils import API_URL
from token_manager import get_valid_token

logger = logging.getLogger(__name__)

# =====================================================
# 設定（環境変数）
# =====================================================

# board API を全面停止したい場合に使用
DISABLE_GET_BOARD = os.getenv("DISABLE_GET_BOARD") == "1"

# 呼び元スタックを出したい場合（デバッグ用）
DEBUG_BOARD_CALLER = os.getenv("DEBUG_BOARD_CALLER") == "1"

# timeout 設定（実戦向け）
DEFAULT_TIMEOUT = 1.5
FORCE_TIMEOUT = 3.0


# =====================================================
# 📈 板情報取得（フル）
# =====================================================
def get_board_info(symbol: str, token: str = None, exchange: int = 1, retry: int = 3):
    """
    銘柄コードの板情報を取得
    - symbol: 銘柄コード（例: "7203"）
    - exchange: 市場コード（1=東証）
    - return: dict（CurrentPrice, UpperLimitPrice, LowerLimitPrice など）
    """

    if DISABLE_GET_BOARD:
        logger.debug(f"[get_board_info] DISABLED symbol={symbol}")
        return None

    if DEBUG_BOARD_CALLER:
        logger.warning(
            "[get_board_info] CALLED FROM:\n%s",
            "".join(traceback.format_stack(limit=6))
        )

    if token is None:
        token = get_valid_token()
    if not token:
        logger.error("❌ トークン取得失敗 → 板情報取得中止")
        return None

    url = f"{API_URL}/board/{symbol}@{exchange}"
    headers = {"X-API-KEY": token}

    for i in range(retry):
        try:
            res = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            if res.status_code == 200:
                data = res.json()
                logger.debug(f"[get_board_info] {symbol}: {data.get('CurrentPrice')}")
                return data
            else:
                logger.warning(
                    f"⚠️ HTTP {res.status_code} ({symbol}): {res.text[:100]}"
                )
        except requests.RequestException as e:
            logger.warning(
                f"⚠️ 板情報通信エラー {symbol} retry={i+1}/{retry}: {e}"
            )
            time.sleep(0.2)
        except Exception as e:
            logger.error(
                f"❌ get_board_info 内部エラー: {symbol} {e}",
                exc_info=True
            )
            break

    logger.warning(f"⚠️ 板情報取得失敗: {symbol} (全{retry}回リトライ後)")
    return None


# =====================================================
# 💹 ベスト気配取得（Ask/Bid のみ）
# =====================================================
def get_best_quotes(symbol: str, exchange: int = 1, retry: int = 3):
    """
    kabuステーションAPIから板情報（ベスト気配）を取得して返す
    戻り値: (best_ask, best_bid, ask_qty, bid_qty)
    """

    if DISABLE_GET_BOARD:
        logger.debug(f"[get_best_quotes] DISABLED symbol={symbol}")
        return None, None, 0, 0

    if DEBUG_BOARD_CALLER:
        logger.warning(
            "[get_best_quotes] CALLED FROM:\n%s",
            "".join(traceback.format_stack(limit=6))
        )

    token = get_valid_token()
    if not token:
        logger.error("❌ トークン取得失敗 → 板情報取得中止")
        return None, None, 0, 0

    endpoint = f"/board/{symbol}@{exchange}"

    for i in range(retry):
        try:
            data = send_request(
                "GET",
                endpoint,
                headers={"X-API-KEY": token},
                timeout=DEFAULT_TIMEOUT
            )
            if not data:
                logger.warning(
                    f"⚠️ 板情報なし: {symbol} retry={i+1}/{retry}"
                )
                time.sleep(0.2)
                continue

            # kabuステーションAPIのキー構造に対応
            best_ask = data.get("AskPrice") or data.get("Sell1Price")
            best_bid = data.get("BidPrice") or data.get("Buy1Price")
            ask_qty  = data.get("AskQty")  or data.get("Sell1Qty")
            bid_qty  = data.get("BidQty")  or data.get("Buy1Qty")

            logger.debug(
                f"[get_best_quotes] {symbol} Ask={best_ask}, Bid={best_bid}"
            )
            return best_ask, best_bid, ask_qty, bid_qty

        except Exception as e:
            logger.warning(
                f"⚠️ 板情報取得失敗: {symbol} retry={i+1}/{retry} ({e})",
                exc_info=False
            )
            time.sleep(0.2)

    logger.warning(f"⚠️ 板情報取得失敗: {symbol} (全{retry}回リトライ後)")
    return None, None, 0, 0


# =====================================================
# 🌐 HTTP共通リクエスト
# =====================================================
def send_request(
    method: str,
    endpoint: str,
    json_data=None,
    headers=None,
    timeout: float = DEFAULT_TIMEOUT
):
    """
    kabuステーションAPIへのHTTPリクエスト共通関数
    """

    if DISABLE_GET_BOARD:
        logger.debug(f"[send_request] DISABLED endpoint={endpoint}")
        return None

    try:
        token = get_valid_token()
        if not token:
            logger.error("❌ 有効なトークンが取得できません")
            return None

        url = API_URL + endpoint
        hdrs = {
            "Content-Type": "application/json",
            "X-API-KEY": token
        }
        if headers:
            hdrs.update(headers)

        logger.debug(f"[send_request] {method} {url} json={json_data}")
        resp = requests.request(
            method,
            url,
            headers=hdrs,
            json=json_data,
            timeout=timeout
        )

        if resp.status_code != 200:
            logger.warning(
                f"⚠️ HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return None

        if resp.text:
            return resp.json()
        return {}

    except requests.RequestException as e:
        logger.error(f"❌ send_request 通信エラー: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"❌ send_request 内部エラー: {e}", exc_info=True)
        return None


# =====================================================
# 🚨 強制板取得（EXIT / 非常用）
# =====================================================
def get_latest_board_force(symbol: str):
    """
    /board を強制取得（pushやキャッシュを使わない）
    ※ EXIT / デバッグ専用
    """

    if DISABLE_GET_BOARD:
        logger.debug(f"[get_latest_board_force] DISABLED symbol={symbol}")
        return None

    try:
        token = get_valid_token()
        if not token:
            return None

        url = f"{API_URL}/board/{symbol}"
        headers = {"X-API-KEY": token}

        res = requests.get(url, headers=headers, timeout=FORCE_TIMEOUT)
        if res.status_code != 200:
            logger.warning(
                f"⚠️ /board取得失敗: {symbol} code={res.status_code}"
            )
            return None

        data = res.json()
        if not data:
            logger.warning(f"⚠️ /board 空データ: {symbol}")
            return None

        ask = data.get("AskPrice")
        bid = data.get("BidPrice")
        cur = data.get("CurrentPrice")

        logger.info(
            f"📈 /board取得成功: {symbol} Ask={ask} Bid={bid} Current={cur}"
        )

        return {
            "AskPrice": ask,
            "BidPrice": bid,
            "CurrentPrice": cur
        }

    except Exception as e:
        logger.error(
            f"❌ get_latest_board_force エラー: {symbol} {e}",
            exc_info=True
        )
        return None
