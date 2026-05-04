# =========================================================
# kabu_api/buy_sell.py（Ver14.9 SAFE Part1）
# =========================================================
import json
import time
import logging
import requests
import configparser
from token_manager import get_valid_token
from utils_common import get_latest_bid_ask, calculate_shares, get_trading_unit
from kabu_api.get_board import get_board_info

API_URL = "http://localhost:18080/kabusapi"
logger = logging.getLogger(__name__)

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")

if not Password:
    logger.warning("⚠ settings.ini の [aukabu] → password が未設定です")


# =========================================================
# 🔥 ストップ高手前判定（空売りENTRYのみ禁止）
# =========================================================
def is_near_upper_limit(board: dict | None) -> bool:
    """ストップ高手前の危険判定"""
    if not board:
        return False

    upper = board.get("UpperLimitPrice")
    current = board.get("CurrentPrice")
    ask = board.get("AskPrice")
    bid = board.get("BidPrice")

    if not upper or not current:
        return False

    # ① 価格がストップ高 -1%以内
    if current >= upper * 0.99:
        return True

    # ② ASK が消滅 → 踏み上げリスク
    if ask is None or ask == 0:
        return True

    # ③ BIDが強すぎる
    if bid and bid >= upper * 0.98:
        return True

    return False


# =========================================================
# 🔹 共通POST処理（リトライ付き）
# =========================================================
def _post_order(payload: dict, token: str) -> str | None:
    """kabuステーションAPI /sendorder POST送信"""
    url = f"{API_URL}/sendorder"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    for attempt in range(3):
        try:
            print("📤 === 送信Payload ===")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print("📤 ==================\n")

            res = requests.post(url, headers=headers, data=json.dumps(payload))

            if res.status_code == 429:
                wait = 1.0 + attempt * 0.5
                logger.warning(f"⚠ 429 Too Many Requests → {wait:.1f}秒後に再試行")
                time.sleep(wait)
                continue

            if res.status_code >= 400:
                logger.error(f"❌ HTTPエラー: {res.status_code} {res.text}")
                time.sleep(1.0)
                continue

            data = res.json()
            print("📥 === レスポンス ===")
            print(json.dumps(data, ensure_ascii=False, indent=2))
            print("📥 ==================\n")

            if "OrderId" in data:
                logger.info(
                    f"✅ 注文成功: {payload['Symbol']} "
                    f"({'買' if payload['Side'] in ['2', 2] else '売'}) "
                    f"{payload['Qty']}株 @ {payload['Price']}円"
                )
                return data["OrderId"]

            logger.error(f"❌ 注文失敗レスポンス: {data}")
            return None

        except Exception as e:
            logger.error(f"❌ リクエスト例外: {e}", exc_info=True)
            time.sleep(1.0)

    logger.error(f"❌ 発注失敗（全リトライ不成立）: {payload.get('Symbol')}")
    return None
