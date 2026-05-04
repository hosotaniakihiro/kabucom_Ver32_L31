#trading/handlers/opening_exit_handler.py

"""
opening_exit_handler.py
寄り付き戦略専用の EXIT ロジック（ティックデータベース）
- 損切り: -0.3%
- 利確  : 最高値から -0.3% 戻り
- 監視  : 9:00〜9:15 のみ
- EXIT注文: 成行（FrontOrderType=10）
"""

import datetime as dt
from kabu_api.close import process_exit
from database.crud import save_trade_history
from utils.alerts_util import send_discord_notify
from utils_common import format_hold_duration

# 各ポジションごとに高値/安値をトラッキング
_price_tracker = {}

def check_and_execute_opening_exit(position, current_price):
    now = dt.datetime.now()
    if not (dt.time(9, 0) <= now.time() <= dt.time(9, 15)):
        return  # 監視時間外はスキップ

    entry_price = position.avg_price
    side = position.side   # "BUY_CREDIT" or "SELL_CREDIT"
    symbol = position.symbol
    symbolname = getattr(position, "symbolname", "")

    stop_loss_ratio = 0.997  # -0.3%
    exit_reason = None

    if side == "BUY_CREDIT":
        # 最高値を更新
        prev_high = _price_tracker.get(symbol, entry_price)
        _price_tracker[symbol] = max(prev_high, current_price)

        # 損切り
        if current_price <= entry_price * stop_loss_ratio:
            exit_reason = "opening_stop_loss"
        # 利確: 最高値から -0.3%
        elif current_price <= _price_tracker[symbol] * stop_loss_ratio:
            exit_reason = "opening_trailing_take_profit"

    elif side == "SELL_CREDIT":
        # 最安値を更新
        prev_low = _price_tracker.get(symbol, entry_price)
        _price_tracker[symbol] = min(prev_low, current_price)

        # 損切り
        if current_price >= entry_price / stop_loss_ratio:  # -0.3% 不利
            exit_reason = "opening_stop_loss"
        # 利確: 最安値から +0.3% 戻り
        elif current_price >= _price_tracker[symbol] / stop_loss_ratio:
            exit_reason = "opening_trailing_take_profit"

    # === EXIT実行 ===
    if exit_reason:
        print(f"📤 Opening EXIT: {symbol} 理由={exit_reason} 成行で発注")
        order_id = process_exit(symbol, side="BUY" if side == "BUY_CREDIT" else "SELL")

        if order_id:
            pnl = (
                (current_price - entry_price) * position.qty
                if side == "BUY_CREDIT"
                else (entry_price - current_price) * position.qty
            )
            hold_duration_str = format_hold_duration(position.open_time, now)

            # DBに保存
            save_trade_history(
                symbol=symbol,
                symbolname=symbolname,
                side="SELL" if side == "BUY_CREDIT" else "BUY",
                action="EXIT",
                qty=position.qty,
                price=current_price,
                order_id=order_id,
                reason=exit_reason,
            )

            # Discord通知
            title = f"🚨 [寄り付きEXIT] {symbolname}({symbol})"
            description = (
                f"理由: {exit_reason}\n"
                f"株数: {position.qty} | 損益: {pnl:+.0f}円\n"
                f"保持期間: {hold_duration_str}\n"
                f"建値: {entry_price} → 現在値: {current_price}"
            )
            send_discord_notify(title, description)

            # EXIT後はトラッキング削除
            if symbol in _price_tracker:
                del _price_tracker[symbol]
