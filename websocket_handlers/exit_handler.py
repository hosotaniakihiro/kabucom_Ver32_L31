import datetime as dt
from database.models import Position
from database import Session_position
from should_exit_position import should_exit_step_trailing_tick
from trade_logic import close_credit_position, update_position_highlow
from utils.alerts_util import send_discord_notify
from utils_common import format_hold_duration
import logging

logger = logging.getLogger("exit_handler")

def check_exit_conditions(content: dict, now: dt.datetime):
    symbol = content.get("Symbol")
    price = content.get("CurrentPrice") or content.get("Price")
    if not (symbol and price):
        return

    price = float(price)
    update_position_highlow(symbol, price)

    session = Session_position()
    pos = session.query(Position).filter_by(symbol=symbol, status="OPEN").first()
    if not pos:
        session.close()
        return

    should_exit, reason, step_level, exit_price = should_exit_step_trailing_tick(symbol, price, pos)
    if should_exit:
        _notify_and_close(pos, price, reason, now)
    session.close()

def handle_trailing_exit(symbol: str, price: float, now: dt.datetime):
    from database import Session_position
    session = Session_position()
    pos = session.query(Position).filter_by(symbol=symbol, status="OPEN").first()
    if pos and getattr(pos, "current_stop", None):
        stop_triggered = (
            (pos.side == "BUY_CREDIT" and price <= pos.current_stop) or
            (pos.side == "SELL_CREDIT" and price >= pos.current_stop)
        )
        if stop_triggered:
            reason = [f"トレーリング発動 {pos.current_stop}円割れ"]
            _notify_and_close(pos, price, reason, now)
    session.close()

def _notify_and_close(pos, price, reason, now):
    pnl = (price - pos.avg_price) * pos.qty if pos.side == "BUY_CREDIT" else (pos.avg_price - price) * pos.qty
    hold_duration_str = format_hold_duration(pos.open_time, now)

    title = f"🚨 [ティックEXIT] {pos.symbolname}({pos.symbol})"
    description = (
        f"理由: {', '.join(reason)}\n"
        f"株数: {pos.qty} | 損益: {pnl:+.0f}円\n"
        f"保持期間: {hold_duration_str}\n"
        f"建値: {pos.avg_price} → 現在値: {price}"
    )
    send_discord_notify(title, description)

    logger.info(f"[WSティックEXIT] close_credit_position 呼び出し {pos.symbol} {pos.qty}")
    success = close_credit_position(symbol=pos.symbol, qty=pos.qty, price=price)

    if success:
        send_discord_notify(
            f"✅ EXIT注文完了: {pos.symbolname}({pos.symbol})",
            f"理由: {', '.join(reason)} | 損益: {pnl:+.0f}円 | 株数: {pos.qty}"
        )
    else:
        send_discord_notify(
            f"⚠️ EXIT注文失敗: {pos.symbolname}({pos.symbol})",
            f"理由: {', '.join(reason)} | 損益: {pnl:+.0f}円 | 株数: {pos.qty}"
        )
