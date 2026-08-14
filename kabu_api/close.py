# ============================================================
# close.py（Ver24-FINAL-SIDE-FIX-REV4-CASH-AND-CREDIT）
# ------------------------------------------------------------
# ✔ Ver23-FINAL-SIDE-FIX-REV3 を原本として完全保持
# ✔ 信用返済 Side ロジック完全維持
# ✔ PositionDB の side 値に100%従う（絶対に逆転しない）
# ✔ Exchange / MarginTradeType / AccountType 完全対応
# ✔ 成行 FrontOrderType=10（返済成行）
# ✔ ClosePositions / orders 配列レスポンス対応
# ✔ ★ 現物（BUY / SELL）EXIT 対応を追加（NEW）
# ✔ ★ process_exit が黙って失敗しない（NEW）
# ============================================================

import json
import time
import logging
import requests
import configparser

from token_manager import get_valid_token
from database.models import Position

API_URL = "http://localhost:18080/kabusapi"
logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# settings.ini パスワード読み込み
# ------------------------------------------------------------
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")
if not Password:
    logger.warning("⚠ settings.ini の [aukabu].password が未設定です")


# ============================================================
# /orders → 約定検索（配列レスポンス対応）
# ============================================================
def _find_execution(order_id: str, token: str):

    url = f"{API_URL}/orders"
    try:
        res = requests.get(
            url,
            headers={"X-API-KEY": token},
            params={"orderId": order_id},
            timeout=5,
        )
    except Exception as e:
        logger.error(f"❌ /orders GET例外: {e}")
        return None

    if res.status_code != 200:
        logger.error(f"❌ /orders HTTP {res.status_code}: {res.text}")
        return None

    arr = res.json()
    if not isinstance(arr, list):
        return None

    for od in arr:
        if str(od.get("OrderId")) != str(order_id):
            continue

        details = od.get("Details") or []
        if not details:
            continue

        d = details[0]
        return {
            "price": d.get("Price"),
            "qty": d.get("Qty"),
            "exec_time": d.get("ExecutionDay"),
        }

    return None


# ============================================================
# 信用返済（ClosePositions）
# ============================================================
def _send_credit_close_order_core(
    symbol, qty, hold_id, side, exchange, margin_type, account_type
):

    token = get_valid_token()
    if not token:
        logger.error("❌ EXIT不可：Token取得失敗")
        return None

    try:
        exchange = int(exchange) if exchange else 1
    except Exception:
        exchange = 1

    try:
        margin_type = int(margin_type) if margin_type else 1
    except Exception:
        margin_type = 1

    try:
        account_type = int(account_type) if account_type else 4
    except Exception:
        account_type = 4

    url = f"{API_URL}/sendorder"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    body = {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": exchange,
        "SecurityType": 1,
        "Side": side,                  # 1=売 / 2=買
        "CashMargin": 3,               # 信用返済
        "MarginTradeType": margin_type,
        "DelivType": 2,
        "AccountType": account_type,
        "Qty": int(qty),
        "FrontOrderType": 10,          # 成行返済
        "Price": 0,
        "ExpireDay": 0,
        "ClosePositions": [
            {"HoldID": hold_id, "Qty": int(qty)}
        ],
    }

    print("\n===== SENDORDER(EXIT-CREDIT) =====")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    print("=================================\n")

    try:
        res = requests.post(url, headers=headers, json=body, timeout=8)
    except Exception as e:
        logger.error(f"❌ EXIT POST例外: {e}", exc_info=True)
        return None

    if res.status_code != 200:
        logger.error(f"❌ EXIT注文エラー {res.status_code}: {res.text}")
        return None

    js = res.json()
    order_id = js.get("OrderId")
    if not order_id:
        logger.error(f"❌ EXIT応答に OrderId が無い: {js}")
        return None

    logger.info(f"🆗 EXIT注文送信成功 OrderId={order_id}")

    for _ in range(10):
        time.sleep(1)
        ex = _find_execution(order_id, token)
        if ex:
            logger.info(f"💹 EXIT 約定: {ex}")
            return {
                "order_id": order_id,
                "exec_price": ex["price"],
                "exec_qty": ex["qty"],
                "exec_time": ex["exec_time"],
            }

    logger.warning("⚠ EXIT 未約定（未検出）")
    return {"order_id": order_id, "exec_price": None, "exec_qty": None, "exec_time": None}


# ============================================================
# 現物 EXIT（NEW）
# ============================================================
def send_cash_close_order(symbol, qty, side, exchange):

    token = get_valid_token()
    if not token:
        logger.error("❌ EXIT不可：Token取得失敗")
        return None

    try:
        exchange = int(exchange) if exchange else 1
    except Exception:
        exchange = 1

    url = f"{API_URL}/sendorder"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    body = {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": exchange,
        "SecurityType": 1,
        "Side": side,              # 1=売 / 2=買
        "CashMargin": 1,           # 現物
        "Qty": int(qty),
        "FrontOrderType": 10,      # 成行
        "Price": 0,
        "ExpireDay": 0,
    }

    print("\n===== SENDORDER(EXIT-CASH) =====")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    print("===============================\n")

    try:
        res = requests.post(url, headers=headers, json=body, timeout=8)
    except Exception as e:
        logger.error(f"❌ CASH EXIT POST例外: {e}", exc_info=True)
        return None

    if res.status_code != 200:
        logger.error(f"❌ CASH EXIT注文エラー {res.status_code}: {res.text}")
        return None

    js = res.json()
    order_id = js.get("OrderId")
    if not order_id:
        logger.error(f"❌ CASH EXIT応答に OrderId が無い: {js}")
        return None

    logger.info(f"🆗 CASH EXIT注文送信成功 OrderId={order_id}")
    return {"order_id": order_id, "exec_price": None, "exec_qty": None, "exec_time": None}


# ============================================================
# Position から EXIT 実行（完全対応）
# ============================================================
def _process_exit_core(position: Position, exit_price: float, reason: str):

    symbol = position.symbol
    qty = position.qty
    hold_id = position.hold_id

    exchange = position.exchange or 1
    margin_type = position.margin_trade_type or 1
    account_type = position.account_type or 4

    logger.info(
        f"🏁 process_exit start symbol={symbol} side={position.side} "
        f"qty={qty} hold_id={hold_id} reason={reason}"
    )

    # ---------------- 信用返済 ----------------
    if position.side == "BUY_CREDIT":
        if not hold_id:
            logger.error(f"❌ BUY_CREDIT に hold_id が無い: {symbol}")
            return None
        return send_credit_close_order(
            symbol, qty, hold_id,
            side=1,  # 売り返済
            exchange=exchange,
            margin_type=margin_type,
            account_type=account_type,
        )

    if position.side == "SELL_CREDIT":
        if not hold_id:
            logger.error(f"❌ SELL_CREDIT に hold_id が無い: {symbol}")
            return None
        return send_credit_close_order(
            symbol, qty, hold_id,
            side=2,  # 買い戻し
            exchange=exchange,
            margin_type=margin_type,
            account_type=account_type,
        )

    # ---------------- 現物 ----------------
    if position.side == "BUY":
        return send_cash_close_order(
            symbol=symbol,
            qty=qty,
            side=1,      # 売り
            exchange=exchange,
        )

    if position.side == "SELL":
        return send_cash_close_order(
            symbol=symbol,
            qty=qty,
            side=2,      # 買い
            exchange=exchange,
        )

    logger.error(f"❌ 未対応の position.side={position.side}")
    return None
# ============================================================
# compatibility layer
# ============================================================

def execute_sell_order(*args, **kwargs):
    """
    旧コード互換
    実際の注文処理が別モジュールの場合でも
    システムを落とさない
    """
    print("[COMPAT] execute_sell_order called")
    return None


def execute_buy_to_close_order(*args, **kwargs):
    """
    信用買い戻し互換
    """
    print("[COMPAT] execute_buy_to_close_order called")
    return None

# ============================================================
# EXIT の 3 段構成
# ------------------------------------------------------------
# 旧構成 (実 install 順で測定):
#   _process_exit_core                    素の実装
#     <- daytrade_credit_force_close      建玉の margin_trade_type を補正
#     <- exit_board_profit_runtime        板を見て利確指値 -> 失敗時フォールバック
#
# daytrade は runtime_env/installer (sitecustomize 経由) で先に入り、
# exit_board は main.py:264 の buy_sell_entry.apply_oneshot_limit_defaults の
# 連鎖で後から入って daytrade ラッパーを掴んでいた。同じ順序で並べてある。
#
# 旧 exit_board は from-import 済みの trading/handlers/exit_handler.process_exit
# にも同じ関数を代入していたが、本文化では close.py 側が最終形を持つので
# 先頭 from-import がそのまま最外周を束縛する。
# (trading/handlers/opening_exit_handler.py も同じ from-import を持つが、
#  リポジトリ全体で import 元が無い到達不能モジュール)
# ============================================================


def _daytrade_credit():
    try:
        from kabu_api import daytrade_credit as _m

        return _m if _m.daytrade_enabled() else None
    except Exception:
        return None


def _exit_board_profit():
    try:
        from kabu_api import exit_board_profit as _m

        return _m if _m.exit_board_enabled() else None
    except Exception:
        return None


def send_credit_close_order(
    symbol, qty, hold_id, side, exchange, margin_type, account_type
):
    _m = _daytrade_credit()
    if _m is None:
        return _send_credit_close_order_core(
            symbol, qty, hold_id, side, exchange, margin_type, account_type
        )
    return _m.send_credit_close_order_respect_margin(
        _send_credit_close_order_core,
        symbol, qty, hold_id, side, exchange, margin_type, account_type,
    )


def _process_exit_daytrade(position: Position, exit_price: float, reason: str):
    """daytrade 層。建玉に margin_trade_type が無ければ補ってから返済する。"""
    _m = _daytrade_credit()
    if _m is not None:
        _m.apply_position_margin_type(position)
    return _process_exit_core(position, exit_price, reason)


def process_exit(position: Position, exit_price: float, reason: str):
    """板利確層 (最外周)。"""
    _m = _exit_board_profit()
    if _m is None:
        return _process_exit_daytrade(position, exit_price, reason)
    return _m.board_exit_then_fallback_or_orig(
        _process_exit_daytrade, position, exit_price, reason
    )
