# trading/logging/exit_logger.py

from datetime import datetime
from database import Session_position
from database.models import ExitLog


def save_exit_log(
    trade_id: str,
    symbol: str,
    entry_price: float,
    exit_price: float,
    entry_time: datetime,
    exit_time: datetime,
    exit_reason: str,
):
    pnl = exit_price - entry_price
    pnl_pct = pnl / entry_price if entry_price else 0.0
    holding_seconds = int((exit_time - entry_time).total_seconds())

    session = Session_position()
    try:
        log = ExitLog(
            trade_id=trade_id,
            symbol=symbol,
            exit_time=exit_time,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_seconds=holding_seconds,
            exit_reason=exit_reason,
        )
        session.add(log)
        session.commit()
    finally:
        session.close()
