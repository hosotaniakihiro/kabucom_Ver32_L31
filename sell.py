# sell.py
from settings import Password
from order_executor import send_order

def execute_credit_sell(symbol, price, qty, hold_id):
    order_body = {
        "Password": Password,
        "Symbol": symbol,
        "Exchange": 1,
        "SecurityType": 1,
        "Side": "1",  # 売り
        "CashMargin": 3,  # 信用返済
        "DelivType": 0,
        "AccountType": 4,
        "Price": price,
        "Qty": qty,
        "FrontOrderType": 20,
        "ExpireDay": 0,
        "ClosePositions": [
            {
                "HoldID": hold_id,
                "Qty": qty
            }
        ]
    }

    print(f"🔴 信用返済売り: {symbol} {qty}株 @ {price}円")
    send_order(order_body)
