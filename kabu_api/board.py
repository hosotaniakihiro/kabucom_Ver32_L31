# kabu_api/board.py
import urllib.request
import json
import time
import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from token_manager import get_valid_token

# kabuステ仕様の市場リスト
MARKET_LIST = ["1", "3", "5", "6"]

_board_cache = {}
_cache_ttl = 5  # 秒


def _try_get_board(symbol, market, token, timeout):
    """内部関数：特定市場で board 取得"""
    url = f"http://localhost:18080/kabusapi/board/{symbol}@{market}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", token)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read())
    except:
        return None


def get_board(symbol: str, retry=3, timeout=5):
    """
    市場コードの自動判定付き board API。
    どの市場でも成功する銘柄を返す。
    """

    now = time.time()

    # --- キャッシュ（高速化） ---
    if symbol in _board_cache:
        data, ts = _board_cache[symbol]
        if now - ts < _cache_ttl:
            return data

    token = get_valid_token()
    if not token:
        print("[BOARD] No valid token")
        return None

    best = None

    # --- 4市場総当たり ---
    for market in MARKET_LIST:
        for i in range(retry):
            result = _try_get_board(symbol, market, token, timeout)
            if result is not None:
                best = result
                break
        if best is not None:
            break

    if best is None:
        print(f"[BOARD ERROR] {symbol} 全市場で取得失敗")
        return None

    # キャッシュ保存
    _board_cache[symbol] = (best, now)

    return best
# ============================================================
# compatibility function
# ============================================================

def get_board_info(*args, **kwargs):
    """
    旧コード互換
    板情報が取得できない場合でも
    システムを落とさない
    """
    return None