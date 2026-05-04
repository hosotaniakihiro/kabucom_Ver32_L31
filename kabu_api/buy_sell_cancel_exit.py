# =========================================================
# kabu_api/buy_sell.py（Ver14.9 SAFE Part3）
# CANCEL & EXIT（返済）部
# =========================================================

# =========================================================
# 🔹 注文状態取得
# =========================================================
def get_order_status(order_id: str):
    """注文ステータスを /orders から取得"""
    try:
        token = get_valid_token()
        url = f"{API_URL}/orders"
        headers = {"X-API-KEY": token}

        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()

        orders = res.json()

        for order in orders:
            if order.get("ID") == order_id:
                return order.get("State") or order.get("OrderState")

        return None

    except Exception as e:
        logger.error(f"get_order_status エラー: {e}", exc_info=True)
        return None


# =========================================================
# 🔹 キャンセル注文
def cancel_order(order_id: str) -> bool:
    """信用注文の未約定キャンセル（正しいAPI方式）"""
    try:
        token = get_valid_token()
        url = f"{API_URL}/orders/{order_id}"
        headers = {"X-API-KEY": token}

        res = requests.delete(url, headers=headers, timeout=5)

        if res.status_code == 200:
            logger.info(f"🟡 キャンセル成功: OrderID={order_id}")
            return True

        # 既にキャンセル済 / 取消不可
        if res.status_code == 409:
            logger.info(f"🟡 既にキャンセル済み: OrderID={order_id}")
            return True

        logger.error(f"❌ キャンセル失敗[{res.status_code}]: {res.text}")
        return False

    except Exception as e:
        logger.error(f"❌ cancel_order エラー: {e}", exc_info=True)
        return False


# =========================================================
# 🔹 ExecutionID EXIT（信用反対売買による実質返済）
#    ※ EXITは常に許可（空売りENTRY禁止の影響なし）
# =========================================================
def execute_credit_exit_opposite(symbol: str, qty: int, exit_side: str, exchange: int = 1):
    """
    EXITは反対売買（CashMargin=2）で実施。
    ENTRYの空売り禁止とは関係なく常に許可される。
    """
    token = get_valid_token()

    payload = {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": exchange,
        "SecurityType": 1,

        # BUY建玉 → EXIT は売り（1）
        # SELL建玉 → EXIT は買い（2）
        "Side": exit_side,

        "CashMargin": 2,              # 信用決済（反対売買）
        "DelivType": 0,
        "AccountType": 4,

        "Qty": int(qty),
        "FrontOrderType": 10,         # 成行
        "Price": 0,
        "ExpireDay": 0
    }

    logger.info(f"🟠 EXIT注文（反対売買）: {symbol} qty={qty} side={exit_side}")
    return _post_order(payload, token)
