# ============================================================
# trade_manager.py
# Ver2.0-PRODUCTION-TRADE-MANAGER-NAS-STABLE
# ------------------------------------------------------------
# ✔ session.py 統合
# ✔ 独立 SQLite engine 廃止
# ✔ Position DB 統合
# ✔ 既存ロジック完全保持
# ✔ NAS安全
# ✔ エントリー / エグジット完全互換
# ============================================================

from datetime import datetime
import math

from database.session import Session_position
from database.models import Position

from order_executor import execute_buy_order
from order_executor import execute_sell_order
from order_executor import is_order_filled

from settings import Token

# ------------------------------------------------------------
# Runtime state
# ------------------------------------------------------------

alert_data = {}

# 投資金額
BUDGET = 500_000


# ============================================================
# Quantity calculation
# ============================================================

def calculate_order_qty(price):

    raw_qty = BUDGET / price
    qty = int(math.floor(raw_qty / 100) * 100)

    return qty if qty >= 100 else 0


# ============================================================
# SQLite log (Position DB)
# ============================================================

def log_trade_to_sqlite(
    symbol,
    symbol_name,
    entry_price,
    shares,
    entry_time,
    exit_price=None,
    profit=None,
    exit_time=None,
    reason_exit=None,
):

    session = Session_position()

    try:

        if exit_price is None:

            trade = Position(
                symbol=symbol,
                symbolname=symbol_name,
                entry_time=entry_time,
                avg_price=entry_price,
                qty=shares,
                status="OPEN",
            )

            session.add(trade)

        else:

            trade = (
                session.query(Position)
                .filter_by(symbol=symbol, status="OPEN")
                .first()
            )

            if trade:

                trade.exit_time = exit_time
                trade.exit_price = exit_price
                trade.status = "CLOSED"

        session.commit()

    except Exception as e:

        session.rollback()
        print(f"❌ SQLite保存エラー: {e}")

    finally:

        session.close()


# ============================================================
# ENTRY
# ============================================================

def handle_entry(symbol, symbol_name, price):

    qty = calculate_order_qty(price)

    if qty == 0:

        print(f"⚠️ {symbol} は株価が高く、50万円で100株未満のため購入スキップ")
        return

    order_id = execute_buy_order(Token, symbol, price, qty)

    alert_data.setdefault(symbol, {})

    alert_data[symbol]["order_id"] = order_id
    alert_data[symbol]["entry_time"] = datetime.now()

    # --------------------------------------------------------
    # 約定確認
    # --------------------------------------------------------

    if is_order_filled(order_id, Token):

        print(f"✅ 注文 {order_id} 約定済")

        alert_data[symbol]["entry_done"] = True
        alert_data[symbol]["entry_price"] = price
        alert_data[symbol]["entry_qty"] = qty
        alert_data[symbol]["highest_price"] = price

        log_trade_to_sqlite(
            symbol,
            symbol_name,
            price,
            qty,
            alert_data[symbol]["entry_time"],
        )

    else:

        print(f"⏳ 注文 {order_id} はまだ約定していません")


# ============================================================
# EXIT
# ============================================================

def check_exit(token, symbol, symbol_name, current_price, ma5=None, ma25=None):

    if symbol not in alert_data:
        return

    if alert_data[symbol].get("entry_done"):

        entry_price = alert_data[symbol].get("entry_price")
        qty = alert_data[symbol].get("entry_qty")
        entry_time = alert_data[symbol].get("entry_time")
        highest_price = alert_data[symbol].get("highest_price", entry_price)
        hold_id = alert_data[symbol].get("hold_id")

        if entry_price is None or qty is None or entry_time is None:

            print(f"⚠️ {symbol} のエントリー情報が不完全")
            return

        # ----------------------------------------------------
        # 高値更新
        # ----------------------------------------------------

        if current_price > highest_price:

            alert_data[symbol]["highest_price"] = current_price
            highest_price = current_price

        # ----------------------------------------------------
        # 計算
        # ----------------------------------------------------

        change_rate = (current_price - entry_price) / entry_price

        drawdown_rate = (
            (current_price - highest_price) / highest_price
            if highest_price > 0
            else 0
        )

        exit_flag = False
        reason = ""

        # ----------------------------------------------------
        # EXIT条件
        # ----------------------------------------------------

        if change_rate >= 0.05:

            reason = "利確（+5%以上）"
            exit_flag = True

        elif change_rate <= -0.03:

            reason = "損切（-3%以上）"
            exit_flag = True

        elif ma5 is not None and current_price < ma5:

            reason = "5MA割れ"
            exit_flag = True

        elif ma25 is not None and abs(current_price - ma25) / ma25 <= 0.005:

            reason = "25MAタッチ"
            exit_flag = True

        elif drawdown_rate <= -0.02:

            reason = "トレーリング利確"
            exit_flag = True

        # ----------------------------------------------------
        # 売却
        # ----------------------------------------------------

        if exit_flag and qty >= 100:

            execute_sell_order(symbol, current_price, qty, hold_id)

            profit = (current_price - entry_price) * qty

            log_trade_to_sqlite(
                symbol=symbol,
                symbol_name=symbol_name,
                entry_price=entry_price,
                shares=qty,
                entry_time=entry_time,
                exit_price=current_price,
                profit=profit,
                exit_time=datetime.now(),
                reason_exit=reason,
            )

            alert_data[symbol]["entry_done"] = False
            alert_data[symbol]["highest_price"] = None

            print(
                f"💰 {symbol_name} ({symbol}) を {reason} により売却 - 損益: {profit:.0f}円"
            )