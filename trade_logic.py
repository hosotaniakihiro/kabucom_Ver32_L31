# trade_logic.py
from kabu_api.positions import get_positions
from kabu_api.orders import check_order_status
import datetime as dt
import time
import logging
import traceback
from database import Session_position
from database.models import Position, TradeHistory
from kabu_api.buy_sell import execute_buy_order,execute_short_order
from kabu_api.close import execute_sell_order,execute_buy_to_close_order
from token_manager import get_valid_token
from utils_common import calculate_shares
from kabu_api.board import get_board_info
from utils_common import get_tick_size
logger = logging.getLogger(__name__)

# === 設定値 ===
TARGET_INVESTMENT = 500_000  # 1エントリーあたり投資金額
TRAILING_STOP_RATIO = 0.98   # 初期逆指値比率（例: 2%下）
MAX_ORDER_POLL_SEC = 5       # 約定確認最大秒数

def handle_entry(token, symbol, symbolname, price, entry_type, df_summary=None, reason_list=None):
    """
    entry_type: BUY_CREDIT / SELL_CREDIT
    reason_list: list of strings（スコア理由）
    """
    if reason_list is None:
        reason_list = ["AUTO_ENTRY"]

    # --- 前足チェック ---
    if not is_previous_candle_valid(symbol, entry_type, df_summary):
        return

    # --- 株数計算・単元株丸め ---
    shares = calculate_shares(price, budget=TARGET_INVESTMENT)
    if shares <= 0:
        print(f"⚠️ 株数0でエントリースキップ: {symbol}")
        return

    # --- 初期トレーリングストップ ---
    init_stop = round(price * TRAILING_STOP_RATIO, 1)

    # --- Position 保存 ---
    session = Session_position()
    hold_id = f"{symbol}_{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    try:
        pos = Position(
            symbol=symbol,
            symbolname=symbolname,
            side=entry_type,
            qty=shares,
            avg_price=price,
            stop_price=init_stop,
            status="OPEN",
            reason_entry=", ".join(reason_list),
            created_at=dt.datetime.now(),
            hold_id=hold_id
        )
        session.add(pos)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"❌ Position保存エラー: {symbol} {e}")
        session.close()
        return
    session.close()

    # --- 発注 ---
    try:
        if entry_type == "BUY_CREDIT":
            order_id = execute_buy_order(symbol, shares, price, token)
        else:
            order_id = execute_short_order(symbol, shares, price, token)
        if order_id:
            print(f"🟢 {entry_type} ORDER: {symbol} {shares}株 @ {price}円 理由: {', '.join(reason_list)}")
        else:
            print(f"❌ 発注失敗: {symbol}")
    except Exception as e:
        print(f"❌ 発注エラー: {symbol} {e}")
        traceback.print_exc()

def close_credit_position(symbol, qty=None, price=None, trigger_price=None, order_id=None, fees=0.0):

    """
    信用取引の返済処理（成行固定）
    - BUY_CREDIT → 売り返済
    - SELL_CREDIT → 買い返済
    """
    session = Session_position()
    now = dt.datetime.now()

    try:
        # === DBからOPENポジションを取得 ===
        pos = session.query(Position).filter(
            Position.symbol == symbol,
            Position.status == "OPEN",
            Position.side.in_(["BUY_CREDIT", "SELL_CREDIT"]),
        ).first()

        if not pos:
            print(f"⚠️ 信用ポジションなし: {symbol}")
            return False

        # qty が指定されていなければ DB の数量を使う
        qty = qty or pos.qty or 0
        if qty <= 0:
            print(f"⚠️ 数量0のため返済不可: {symbol}")
            return False

        print(f"📤 EXIT発注準備: {symbol} {qty}株 side={pos.side}")

        # === API側の建玉を取得 ===
        api_positions = get_positions() or []
        api_pos = next((p for p in api_positions if str(p.get("Symbol")) == str(symbol)), None)

        if not api_pos:
            print(f"⚠️ API側に建玉が見つからないため返済不可: {symbol}")
            return False

        hold_id = api_pos.get("HoldID") or pos.hold_id
        if not hold_id:
            print(f"⚠️ HoldID不明のため返済不可: {symbol}")
            return False

        # === API注文用ペイロード ===
        payload = {
            "Password": API_PASSWORD,
            "Symbol": symbol,
            "Exchange": 1,
            "SecurityType": 1,
            "Side": "1" if pos.side == "BUY_CREDIT" else "2",  # 1=売り返済, 2=買い返済
            "CashMargin": 3,
            "MarginTradeType": 1,
            "DelivType": 2,
            "AccountType": 4,
            "Qty": qty,
            "FrontOrderType": 10,  # 成行
            "Price": 0,
            "ExpireDay": 0,
            "ClosePositions": [
                {"HoldID": hold_id, "Qty": qty}
            ]
        }

        print(f"📤 送信ペイロード: {payload}")

        res = requests.post(
            f"{API_URL}/sendorder",
            headers={"Content-Type": "application/json", "X-API-KEY": API_KEY},
            json=payload,
            timeout=5
        )

        print(f"📥 HTTPステータス: {res.status_code}")
        if res.status_code != 200:
            print(f"❌ 売り注文エラー: {res.status_code} {res.reason}")
            print(f"レスポンス本文: {res.json()}")
            return False

        result = res.json()
        order_id = result.get("OrderId")
        print(f"✅ 信用返済注文送信成功: {symbol} {qty}株 ID={order_id}")

        # === DB更新 ===
        pos.status = "CLOSED"
        pos.close_time = now
        pos.close_price = price or pos.avg_price
        pos.fees = fees
        session.commit()

        return True

    except Exception as e:
        print(f"❌ 信用返済処理エラー: {e}")
        session.rollback()
        return False

    finally:
        session.close()

def is_previous_candle_valid(symbol, side, df_summary):
    if df_summary is None or df_summary.empty:
        return True
    today = dt.datetime.now().date()
    df_today = df_summary[(df_summary["symbol"] == str(symbol)) & (df_summary["date"] == today)]
    if df_today.empty or len(df_today) < 2:
        return True
    prev_candle = df_today.iloc[-2]
    open_price = prev_candle["open_price"]
    close_price = prev_candle["close_price"]
    is_bullish = close_price > open_price
    is_bearish = close_price < open_price
    if side == "BUY_CREDIT" and is_bearish:
        print(f"⚠️ {symbol} 買いエントリーNG（直前足が陰線）")
        return False
    if side == "SELL_CREDIT" and is_bullish:
        print(f"⚠️ {symbol} 売りエントリーNG（直前足が陽線）")
        return False
    return True

def store_trade_history(symbol, symbolname, side, action, qty, price, order_id, position_id, realized_pnl=0.0, fees=0.0):
    session = Session_position()
    try:
        hist = TradeHistory(
            symbol=symbol,
            symbolname=symbolname,
            side=side,
            action=action,
            qty=qty,
            price=price,
            order_id=order_id,
            position_id=position_id,
            realized_pnl=realized_pnl,
            fees=fees,
            created_at=dt.datetime.now()
        )
        session.add(hist)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"❌ TradeHistory保存失敗: {symbol} {e}")
    finally:
        session.close()

def update_position_highlow(symbol, latest_price):
    """ポジションの最高値/最安値を更新"""
    session = Session_position()
    try:
        pos = session.query(Position).filter_by(symbol=symbol, status="OPEN").first()
        if not pos:
            return

        if pos.side == "BUY_CREDIT":
            if pos.highest_price is None or latest_price > pos.highest_price:
                pos.highest_price = latest_price
        elif pos.side == "SELL_CREDIT":
            if pos.lowest_price is None or latest_price < pos.lowest_price:
                pos.lowest_price = latest_price

        session.commit()
    except Exception as e:
        print(f"⚠️ 最高値/最安値更新エラー: {e}")
        session.rollback()
    finally:
        session.close()

def handle_exit(symbol, qty=None, reason=None, fees=0.0):
    """
    EXIT処理を一括で行う
    - symbol: 銘柄コード
    - qty: 返済数量（Noneなら全数量）
    - reason: EXIT理由を記録するための文字列
    - fees: 手数料
    戻り値:
        dict(success=True/False, order_id=..., price=..., qty=..., error=...)
    """
    session = Session_position()
    now = dt.datetime.now()

    try:
        pos = (
            session.query(Position)
            .filter(Position.symbol == symbol, Position.status == "OPEN")
            .first()
        )

        if not pos:
            msg = f"⚠️ EXIT対象ポジションなし: {symbol}"
            print(msg)
            logger.warning(msg)
            return {"success": False, "error": "no_open_position"}

        exit_qty = qty or pos.qty
        logger.info(f"📤 EXIT発注準備: {symbol} {exit_qty}株 side={pos.side}")

        # === 信用返済処理呼び出し ===
        result = close_credit_position(symbol, price=None, fees=fees)
        if not result.get("success"):
            logger.error(f"❌ 信用返済失敗: {symbol} error={result.get('error')}")
            return result

        # === DB更新 ===
        pos.status = "CLOSED"
        pos.close_time = now
        pos.exit_price = result.get("price", pos.avg_price)
        pos.realized_pnl = (
            (pos.exit_price - pos.avg_price) * pos.qty
            if pos.side == "BUY_CREDIT"
            else (pos.avg_price - pos.exit_price) * pos.qty
        )
        pos.reason_exit = reason or "AUTO_EXIT"
        pos.fees = fees
        session.commit()

        logger.info(
            f"✅ EXIT完了: {symbol} {exit_qty}株 @ {pos.exit_price} 損益={pos.realized_pnl:+.0f}円"
        )

        return {
            "success": True,
            "order_id": result.get("order_id"),
            "price": pos.exit_price,
            "qty": exit_qty,
            "pnl": pos.realized_pnl,
        }

    except Exception as e:
        session.rollback()
        logger.error(f"❌ handle_exit エラー: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

    finally:
        session.close()

def place_entry_order(symbol: str, side: str, qty: int, token: str):
    """
    板情報を確認して +1ティックずらした指値で発注する
    """
    board = get_board_info(symbol, token)
    if not board:
        return False, "板情報取得失敗"

    best_bid = board.get("BidPrice")
    best_bid_qty = board.get("BidQty")
    best_ask = board.get("AskPrice")
    best_ask_qty = board.get("AskQty")

    # --- 流動性チェック ---
    if not best_bid or not best_ask or best_bid_qty < 100 or best_ask_qty < 100:
        return False, "流動性不足でスキップ"

    # --- ティックサイズを決定 ---
    tick_size = get_tick_size(best_ask if side == "BUY_CREDIT" else best_bid)

    # --- 発注価格を決定 (+1ティックずらし) ---
    if side == "BUY_CREDIT":
        order_price = best_ask + tick_size
        order_id = execute_buy_order(symbol, qty, price=order_price)
    elif side == "SELL_CREDIT":
        order_price = best_bid - tick_size
        order_id = execute_short_order(symbol, qty, price=order_price)
    else:
        return False, f"未対応のside: {side}"

    return (True, f"注文成功: {order_id} {symbol} {side} {qty}@{order_price}") if order_id else (False, "注文失敗")

def update_position_trailing_stop(symbol, latest_price, trail_pct=0.003):
    """
    トレーリングストップの更新
    - trail_pct: 0.003 = 0.3%
    - BUY_CREDIT: 高値更新時にストップを引き上げ
    - SELL_CREDIT: 安値更新時にストップを引き下げ
    """
    session = Session_position()
    try:
        pos = session.query(Position).filter_by(symbol=symbol, status="OPEN").first()
        if not pos:
            return

        if pos.side == "BUY_CREDIT":
            if pos.highest_price is None or latest_price > pos.highest_price:
                pos.highest_price = latest_price
            new_stop = latest_price * (1 - trail_pct)
            if not pos.current_stop or new_stop > pos.current_stop:
                pos.current_stop = round(new_stop, 1)

        elif pos.side == "SELL_CREDIT":
            if pos.lowest_price is None or latest_price < pos.lowest_price:
                pos.lowest_price = latest_price
            new_stop = latest_price * (1 + trail_pct)
            if not pos.current_stop or new_stop < pos.current_stop:
                pos.current_stop = round(new_stop, 1)

        session.commit()
    except Exception as e:
        print(f"⚠️ update_position_trailing_stop エラー: {e}")
        session.rollback()
    finally:
        session.close()
