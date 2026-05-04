#database/crud/crud_trade.py

import datetime as dt
from database.models import Position, TradeHistory
import pandas as pd
import logging
from sqlalchemy.exc import SQLAlchemyError
from ..session import Session_position
from ..models import TradeHistory
logger = logging.getLogger(__name__)

def store_trade_history(symbol, symbolname, side, action, qty, price,
                        order_id=None, position_id=None, realized_pnl=0.0, fees=0.0):
    """TradeHistory 保存"""
    session = Session_position()
    try:
        trade = TradeHistory(
            symbol=symbol,
            symbolname=symbolname,
            side=side,
            action=action,
            qty=qty,
            price=price,
            order_id=order_id,
            position_id=position_id,
            trade_time=dt.datetime.utcnow(),
            realized_pnl=realized_pnl,
            fees=fees,
        )
        session.add(trade)
        session.commit()
        logger.info(f"💾 TradeHistory保存: {symbol} {side} {action} {qty}@{price}")
    except Exception as e:
        session.rollback()
        logger.error(f"❌ TradeHistory保存失敗: {e}", exc_info=True)
    finally:
        session.close()

def store_trade_and_update_position(symbol, symbolname, side, qty, price, hold_id=None, fees=0.0):
    """新規ポジション作成 or 分割約定更新"""
    session = Session_position()
    now = dt.datetime.now()
    try:
        pos = session.query(Position).filter(
            Position.symbol == symbol,
            Position.side == side,
            Position.status == "OPEN",
        ).first()

        invested_amount = qty * price
        if pos:
            total_qty = pos.qty + qty
            pos.avg_price = ((pos.avg_price * pos.qty) + invested_amount) / total_qty
            pos.qty = total_qty
            pos.invested_amount += invested_amount
            pos.hold_id = hold_id or pos.hold_id
            pos.open_time = pos.open_time or now
            session.commit()
            logger.info(f"🔄 分割約定集約: {symbol} 数量={pos.qty}, 単価={pos.avg_price:.2f}")
        else:
            new_pos = Position(
                symbol=symbol,
                symbolname=symbolname,
                side=side,
                qty=qty,
                avg_price=price,
                invested_amount=invested_amount,
                hold_id=hold_id,
                status="OPEN",
                open_time=now,
                fees_accum=fees,
            )
            session.add(new_pos)
            session.commit()
            logger.info(f"🆕 新規ポジション作成: {symbol} 数量={qty}, 単価={price}")
    except Exception as e:
        session.rollback()
        logger.error(f"❌ store_trade_and_update_position エラー: {e}", exc_info=True)
    finally:
        session.close()

def get_open_positions():
    """OPEN 状態のポジション一覧を取得"""
    session = Session_position()
    try:
        return session.query(Position).filter(Position.status == "OPEN").all()
    except SQLAlchemyError as e:
        logger.error(f"❌ get_open_positions エラー: {e}", exc_info=True)
        return []
    finally:
        session.close()

def save_trade_history(symbol, symbolname, side, qty, price, order_id, reason="opening_entry"):
    """ENTRY専用のトレード履歴保存"""
    session = Session_position()
    try:
        trade = TradeHistory(
            symbol=symbol,
            symbolname=symbolname,
            side=side,
            action="ENTRY",
            qty=qty,
            price=price,
            order_id=order_id,
            position_id=None,
            trade_time=dt.datetime.utcnow(),
            realized_pnl=0.0,
            fees=0.0,
            reason=reason,
        )
        session.add(trade)
        session.commit()
        logger.info(f"💾 TradeHistory保存成功: {symbol} {side} {qty}株 @ {price}")
        return trade.id
    except Exception as e:
        session.rollback()
        logger.error(f"❌ TradeHistory保存失敗: {e}", exc_info=True)
        return None
    finally:
        session.close()

def sync_positions_with_api(positions):
    """
    APIの建玉一覧とDBのPositionを同期
    - APIのSymbolNameを信頼して保存
    - APIに存在しない建玉はCLOSED扱いに更新
    - APIに存在するがDBにない建玉は新規追加
    """
    session = Session_position()
    side_map = {"1": "SELL_CREDIT", "2": "BUY_CREDIT"}

    try:
        api_symbols = set()

        # === APIから渡された建玉を処理 ===
        for p in positions:
            symbol = str(p.get("Symbol"))
            symbolname = p.get("SymbolName", "")  # API側をそのまま信頼
            side = side_map.get(str(p.get("Side")), "UNKNOWN")
            qty = int(p.get("LeavesQty", 0))
            avg_price = float(p.get("Price", 0.0) or 0.0)
            invested_amount = avg_price * qty
            hold_id = p.get("ExecutionID")

            api_symbols.add((symbol, hold_id))

            # === 既存ポジションを更新 or 新規追加 ===
            existing = session.query(Position).filter_by(symbol=symbol, hold_id=hold_id).first()
            if existing:
                existing.qty = qty
                existing.avg_price = avg_price
                existing.invested_amount = invested_amount
                existing.symbolname = symbolname
                existing.side = side
                existing.status = "OPEN" if qty > 0 else "CLOSED"
            else:
                new_pos = Position(
                    symbol=symbol,
                    symbolname=symbolname,
                    side=side,
                    qty=qty,
                    avg_price=avg_price,
                    invested_amount=invested_amount,
                    hold_id=hold_id,
                    status="OPEN" if qty > 0 else "CLOSED",
                    open_time=dt.datetime.utcnow(),
                )
                session.add(new_pos)

        # === APIに存在しない → DBをCLOSEDに更新 ===
        db_positions = session.query(Position).filter(Position.status == "OPEN").all()
        for pos in db_positions:
            if (pos.symbol, pos.hold_id) not in api_symbols:
                pos.status = "CLOSED"
                pos.close_time = dt.datetime.utcnow()
                logger.info(f"🔒 ポジションCLOSED: {pos.symbol} HoldID={pos.hold_id}")

        session.commit()
        logger.info("✅ ポジションDBとAPIの整合完了")

    except Exception as e:
        session.rollback()
        logger.error(f"❌ ポジション同期エラー: {e}", exc_info=True)
    finally:
        session.close()
