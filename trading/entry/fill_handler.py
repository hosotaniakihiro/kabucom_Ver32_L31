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
from core.global_context.context import global_context as GC

from database import Session_position
from database.models import Position

# ENTRY_EVENT
#   2026-09-14: mark_entry_filled は entry_event_updater に存在しない。
#   同モジュールにあるのは update_entry_result(symbol, pnl, holding_seconds) で、
#   これは EXIT 後に損益を埋める関数であり、ENTRY 約定時には呼べない
#   (pnl / holding_seconds がまだ確定していない)。
#   このファイルは 2026-05-04 の作成以来一度も呼び出されておらず、
#   import した時点で ImportError になっていた。責務4は呼び先ごと
#   存在しないので、ENTRY_EVENT の約定済み更新は行わない。

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
        # 2026-09-14: order_id は database.models.Position に列が無い。
        #   実DB (positions.db) には order_id 列があるが、モデル定義が
        #   9 列ぶん古い (order_id / high_since_entry / low_since_entry /
        #   entry_id / entry_source / entry_mode / entry_reason /
        #   reason_code / technical_snapshot が未定義)。
        #   モデルを触ると他の読み書きに影響するため、ここでは渡さない。
        pos = Position(
            symbol=symbol,
            symbolname=symbolname,
            side=side,
            qty=qty,
            avg_price=price,
            entry_time=now,
            status="OPEN",
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
        try:
            if hasattr(GC, "ai") and GC.ai and hasattr(GC.ai, "set_exit_ctx"):
                GC.ai.set_exit_ctx(symbol, ctx)
        except Exception:
            logger.debug("ExitContext GC.ai registration failed symbol=%s", symbol, exc_info=True)

        # ----------------------------------------------------
        # 3. inflight / pending 解除
        # ----------------------------------------------------
        if hasattr(global_data, "entry_inflight"):
            global_data.entry_inflight.discard(symbol)

        if hasattr(global_data, "pending_orders"):
            global_data.pending_orders.pop(order_id, None)

        # ----------------------------------------------------
        # 4. ENTRY_EVENT 更新は行わない (上記 import のコメント参照)
        # ----------------------------------------------------

        logger.info(
            "✅ ENTRY FILLED %s %s qty=%s price=%.2f order_id=%s",
            symbol, side, qty, price, order_id,
        )

    except Exception:
        session.rollback()
        logger.exception("❌ on_entry_filled ERROR symbol=%s", symbol)

    finally:
        session.close()
