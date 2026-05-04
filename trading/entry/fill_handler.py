# ============================================================
# trading/entry/fill_handler.py
# Ver1.0.0-FINAL-ENTRY-FILL-HANDLER
# ------------------------------------------------------------
# ✔ ENTRY 約定後の唯一の処理点
# ✔ Position OPEN
# ✔ ExitContext 生成（factory 経由）
# ✔ pending / inflight 完全解除
# ✔ ENTRY_EVENT を約定済みに更新
# ✔ exit_controller による監視開始トリガ
# ✔ 副作用はここに集約
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from global_state import global_data

from database import Session_position
from database.models import Position

# ENTRY_EVENT
from trading.entry.entry_event_updater import (
    mark_entry_filled,
)

# EXIT CONTEXT
from trading.exit.exit_context_factory import create_exit_context

logger = logging.getLogger("entry_fill_handler")


# ============================================================
# ENTRY 約定イベント（唯一の入口）
# ============================================================

def on_entry_filled(
    *,
    symbol: str,
    symbolname: str,
    side: str,                     # BUY / SELL
    qty: int,
    price: float,
    atr_1min: float,
    order_id: str,
    filled_time: Optional[dt.datetime] = None,
):
    """
    ENTRY 約定後に必ず呼ばれる唯一の関数

    ※ 判断・EXIT・AI は一切行わない
    """

    now = filled_time or dt.datetime.now()
    session = Session_position()

    try:
        # ----------------------------------------------------
        # 1. Position OPEN
        # ----------------------------------------------------
        pos = Position(
            symbol=symbol,
            symbolname=symbolname,
            side=side,
            qty=qty,
            avg_price=price,
            entry_time=now,
            status="OPEN",
            order_id=order_id,
        )
        session.add(pos)
        session.commit()

        # ----------------------------------------------------
        # 2. ExitContext 生成（factory）
        # ----------------------------------------------------
        ctx = create_exit_context(
            symbol=symbol,
            side=side,
            entry_price=price,
            atr_1min=atr_1min,
            entry_time=now,
        )

        global_data.exit_ctx = getattr(global_data, "exit_ctx", {})
        global_data.exit_ctx[symbol] = ctx

        # ----------------------------------------------------
        # 3. inflight / pending 解除
        # ----------------------------------------------------
        if hasattr(global_data, "entry_inflight"):
            global_data.entry_inflight.discard(symbol)

        if hasattr(global_data, "pending_orders"):
            global_data.pending_orders.pop(order_id, None)

        # ----------------------------------------------------
        # 4. ENTRY_EVENT を約定済みに更新
        # ----------------------------------------------------
        mark_entry_filled(
            symbol=symbol,
            order_id=order_id,
            filled_price=price,
            filled_time=now,
        )

        logger.info(
            "✅ ENTRY FILLED %s %s qty=%s price=%.2f order_id=%s",
            symbol, side, qty, price, order_id,
        )

    except Exception:
        session.rollback()
        logger.exception("❌ on_entry_filled ERROR symbol=%s", symbol)

    finally:
        session.close()
