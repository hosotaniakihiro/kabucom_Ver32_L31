# ============================================================
# buy_sell.py（Ver23-FINAL + ORDER-WRAPPER）
# ------------------------------------------------------------
# ・信用新規注文（BUY_CREDIT / SELL_CREDIT）
# ・df_board / df_push_all から bid/ask/exchange を取得
# ・Kabustation API sendorder
# ・best-ask / best-bid 指値発注
# ・上位互換ラッパー execute_*_order 追加
# ============================================================

import json
import logging
import time
import requests
import configparser
import pandas as pd

from token_manager import get_valid_token
from global_state import global_data

API_URL = "http://localhost:18080/kabusapi"
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 設定ロード
# ------------------------------------------------------------
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")

if not Password:
    logger.warning("⚠ settings.ini の [aukabu].password が未設定です")

# ------------------------------------------------------------
# 🔍 DataFrame から bid/ask/exchange を取得
# ------------------------------------------------------------
def _get_board_info(symbol: str):
    """
    df_board → df_push_all の順で bid/ask/exchange を探す
    """

    # --- df_board（最優先） ---
    df = getattr(global_data, "df_board", None)
    if isinstance(df, pd.DataFrame) and not df.empty:
        d = df[df.get("symbol") == symbol]
        if not d.empty:
            row = d.iloc[-1]
            return {
                "bid": float(row.get("bid_price", 0)),
                "ask": float(row.get("ask_price", 0)),
                "exchange": int(row.get("exchange", 1)),
            }

    # --- pushDB（df_push_all） ---
    df = getattr(global_data, "df_push_all", None)
    if isinstance(df, pd.DataFrame) and not df.empty:
        d = df[df.get("Symbol") == symbol]
        if not d.empty:
            row = d.iloc[-1]
            return {
                "bid": float(row.get("BidPrice", 0)),
                "ask": float(row.get("AskPrice", 0)),
                "exchange": 1,
            }

    logger.warning(f"⚠ 板情報が見つかりません: {symbol}")
    return None


# ------------------------------------------------------------
# 📤 sendorder POST（OrderIdまで取得）
# ------------------------------------------------------------
def _post_order(payload: dict, token: str):

    url = f"{API_URL}/sendorder"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token
    }

    print("\n================ SENDORDER ==================")
    print("📤 Payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("==============================================")

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"❌ sendorder 例外: {e}", exc_info=True)
        return None

    print(f"📥 HTTP STATUS: {res.status_code}")
    print(f"📥 RESPONSE: {res.text}")

    if res.status_code != 200:
        logger.error(f"❌ sendorder エラー: {res.status_code} {res.text}")
        return None

    data = res.json()
    oid = data.get("OrderId")

    if not oid:
        logger.error(f"❌ OrderId がありません: {data}")
        return None

    logger.info(f"✅ SENDORDER SUCCESS: OrderId={oid}")
    return oid


# ------------------------------------------------------------
# 🟢 信用新規 BUY（best-ask）
# ------------------------------------------------------------
def execute_buy_at_best_ask(symbol: str, qty: int):
    """
    best-ask 指値で信用新規買い
    return: (order_id, executed_price)
    """
    token = get_valid_token()
    if not token:
        logger.error("❌ トークン取得失敗")
        return None, None

    info = _get_board_info(symbol)
    if not info:
        return None, None

    ask = info["ask"]
    exchange = info["exchange"]

    if ask <= 0:
        logger.warning(f"⚠ ASK 不明 → 成行")
        price = 0
        fot = 10
    else:
        price = int(ask)
        fot = 20

    body = {
        "Password": Password,
        "Symbol": symbol,
        "Exchange": exchange,
        "SecurityType": 1,
        "Side": 2,                   # BUY_CREDIT
        "CashMargin": 2,
        "MarginTradeType": "1",
        "DelivType": 0,
        "AccountType": 4,
        "Qty": int(qty),
        "FrontOrderType": fot,
        "Price": price,
        "ExpireDay": 0,
    }

    logger.info(f"🟢 BUY_CREDIT {symbol} ask={ask} qty={qty} EX={exchange}")
    oid = _post_order(body, token)
    return oid, price if oid else None


# ------------------------------------------------------------
# 🔻 信用新規 SELL（best-bid）
# ------------------------------------------------------------
def execute_short_at_best_bid(symbol: str, qty: int):
    """
    best-bid 指値で信用新規売り
    return: (order_id, executed_price)
    """
    token = get_valid_token()
    if not token:
        logger.error("❌ トークン取得失敗")
        return None, None

    info = _get_board_info(symbol)
    if not info:
        return None, None

    bid = info["bid"]
    exchange = info["exchange"]

    if bid <= 0:
        logger.warning(f"⚠ BID 不明 → 成行")
        price = 0
        fot = 10
    else:
        price = int(bid)
        fot = 20

    body = {
        "Password": Password,
        "Symbol": symbol,
        "Exchange": exchange,
        "SecurityType": 1,
        "Side": 1,                   # SELL_CREDIT
        "CashMargin": 2,
        "MarginTradeType": "1",
        "DelivType": 0,
        "AccountType": 4,
        "Qty": int(qty),
        "FrontOrderType": fot,
        "Price": price,
        "ExpireDay": 0,
    }

    logger.info(f"🔻 SELL_CREDIT {symbol} bid={bid} qty={qty} EX={exchange}")
    oid = _post_order(body, token)
    return oid, price if oid else None
# ------------------------------------------------------------
# 🟢 信用新規 BUY（逆指値：Stop Buy）
# ------------------------------------------------------------
# ============================================================
# 🟢 信用新規 BUY（逆指値：上抜け）
# ============================================================
def execute_buy_stop(symbol: str, qty: int, stop_price: float):
    """
    STOP_LIMIT BUY
    stop_price を超えたら発注される
    """
    token = get_valid_token()
    if not token:
        logger.error("❌ トークン取得失敗")
        return None

    info = _get_board_info(symbol)
    if not info:
        return None

    exchange = info["exchange"]

    body = {
        "Password": Password,
        "Symbol": symbol,
        "Exchange": exchange,
        "SecurityType": 1,
        "Side": 2,                   # BUY_CREDIT
        "CashMargin": 2,
        "MarginTradeType": "1",
        "DelivType": 0,
        "AccountType": 4,
        "Qty": int(qty),
        "FrontOrderType": 30,        # ★ STOP_LIMIT
        "Price": int(stop_price),    # 指値（通常 stop と同値）
        "StopPrice": int(stop_price),
        "ExpireDay": 0,
    }

    logger.info(f"🟢 STOP BUY {symbol} stop={stop_price} qty={qty}")
    oid = _post_order(body, token)
    return (oid, stop_price) if oid else None


# ============================================================
# 🔻 信用新規 SELL（逆指値：下抜け）
# ============================================================
def execute_short_stop(symbol: str, qty: int, stop_price: float):
    """
    STOP_LIMIT SELL
    stop_price を下回ったら発注される
    """
    token = get_valid_token()
    if not token:
        logger.error("❌ トークン取得失敗")
        return None

    info = _get_board_info(symbol)
    if not info:
        return None

    exchange = info["exchange"]

    body = {
        "Password": Password,
        "Symbol": symbol,
        "Exchange": exchange,
        "SecurityType": 1,
        "Side": 1,                   # SELL_CREDIT
        "CashMargin": 2,
        "MarginTradeType": "1",
        "DelivType": 0,
        "AccountType": 4,
        "Qty": int(qty),
        "FrontOrderType": 30,        # ★ STOP_LIMIT
        "Price": int(stop_price),
        "StopPrice": int(stop_price),
        "ExpireDay": 0,
    }

    logger.info(f"🔻 STOP SELL {symbol} stop={stop_price} qty={qty}")
    oid = _post_order(body, token)
    return (oid, stop_price) if oid else None


# ============================================================
# ★ 上位互換ラッパー（既存コード互換用）
# ============================================================

def execute_buy_order(symbol: str, price: float, qty: int, token=None):
    """
    entry_executor 互換ラッパー
    ※ price / token は無視（板最良を常に使用）
    """
    oid, _ = execute_buy_at_best_ask(symbol, qty)
    return oid


def execute_short_order(symbol: str, price: float, qty: int, token=None):
    """
    entry_executor 互換ラッパー
    ※ price / token は無視（板最良を常に使用）
    """
    oid, _ = execute_short_at_best_bid(symbol, qty)
    return oid
