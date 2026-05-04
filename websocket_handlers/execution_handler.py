import logging
from database import Session_position
from database.models import Position
from database.crud import store_trade_and_update_position

logger = logging.getLogger("execution_handler")

def handle_execution(content: dict):
    try:
        symbol = content.get("Symbol")
        symbolname = content.get("SymbolName", "不明")
        side = "BUY_CREDIT" if str(content.get("Side")) == "2" else "SELL_CREDIT"
        qty = int(content.get("Qty", 0))
        price = float(content.get("Price", 0))
        hold_id = str(content.get("ExecutionID"))

        session = Session_position()
        pos = (
            session.query(Position)
            .filter_by(symbol=symbol, side=side, status="OPEN", hold_id=None)
            .order_by(Position.open_time.desc())
            .first()
        )
        if pos:
            pos.hold_id = hold_id
            session.commit()
            logger.info(f"✅ Position更新: {symbol} {qty}株 @ {price} → HoldID={hold_id}")
        else:
            store_trade_and_update_position(
                symbol=symbol,
                symbolname=symbolname,
                side=side,
                qty=qty,
                price=price,
                hold_id=hold_id,
            )
            logger.info(f"✅ Position新規保存: {symbol} {qty}株 @ {price} (HoldID={hold_id})")
        session.close()
    except Exception as e:
        logger.error(f"[WS約定] ❌ 約定処理エラー: {e}", exc_info=True)
