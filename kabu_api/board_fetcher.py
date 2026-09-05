# ============================================================
# board_fetcher.py（Ver24-SAFE / API429 回避用）
# ------------------------------------------------------------
# ・1秒ごとに ATS 登録銘柄の board を取得しキャッシュに保存
# ・entry_controller は API を叩かず board_cache のみ参照
# ============================================================

import time
import threading
import json
import sys
import os

from token_manager import get_valid_token
from global_state import global_data

from kabu_api import kabu_http

# kabuステ市場コード一覧
MARKETS = ["1", "3", "5", "6"]


def try_board(symbol, market, token):
    """特定市場で board を試行取得

    urllib.request の "Connection: close" では kabu Station が応答を
    返さない (詳細は kabu_api/kabu_http.py)。timeout=2 のまま無応答に
    なると 1 秒周期のループが銘柄数×市場数ぶん詰まる。
    """
    url = f"http://localhost:18080/kabusapi/board/{symbol}@{market}"
    try:
        return kabu_http.request_json(url, method="GET", api_key=token, timeout=2)
    except Exception:
        return None


def board_fetch_loop():
    """1秒ごとに ATS登録銘柄の board を更新するメインループ"""
    print("🚀 board_fetcher 起動（1秒周期でboard更新）")

    while True:
        symbols = list(global_data.registered_symbols)
        if not symbols:
            time.sleep(1)
            continue

        token = get_valid_token()
        now = time.time()

        for sym in symbols:
            board = None

            # 4市場総当たり
            for m in MARKETS:
                board = try_board(sym, m, token)
                if board:
                    break

            # キャッシュ保存
            global_data.board_cache[sym] = {
                "board": board,
                "ts": now
            }

        time.sleep(1)
