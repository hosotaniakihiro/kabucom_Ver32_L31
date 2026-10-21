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
import os
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


def _to_db_side(side: str, *, margin_trade_type, hold_id) -> str:
    """建玉 dict の side (BUY/SELL) を positions.db の側 (BUY_CREDIT/SELL_CREDIT) へ。

    信用建玉の判定は trading/exit/order_sender._is_credit_position と同じ基準
    (hold_id があるか margin_trade_type があるか)。現物なら BUY/SELL のまま。
    """
    s = str(side or "").strip().upper()
    if s in {"BUY_CREDIT", "SELL_CREDIT"}:
        return s
    if s in {"LONG", "2"}:
        s = "BUY"
    elif s in {"SHORT", "1"}:
        s = "SELL"
    is_credit = bool(hold_id) or margin_trade_type not in (None, "", 0, "0")
    if is_credit and s == "BUY":
        return "BUY_CREDIT"
    if is_credit and s == "SELL":
        return "SELL_CREDIT"
    return s or "BUY"


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
    exchange: Optional[int] = None,
    margin_trade_type: Optional[int] = None,
    account_type: Optional[int] = None,
    hold_id: str = "",
    execution_id: str = "",
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
        # 2026-09-14 後場の実害: side=BUY / hold_id なしで書いた行を決済側が
        #   ブローカー情報より優先して採用し、信用建玉に現物売り
        #   (CashMargin=1) を送って 4001005 で失敗し続けた。
        #   決済 (kabu_api.close._process_exit_core) は side が BUY_CREDIT /
        #   SELL_CREDIT かつ hold_id があるときだけ信用返済 (CashMargin=3,
        #   ClosePositions[].HoldID) を送る。HoldID は /positions の
        #   ExecutionID と同じ値なので、建玉から来た execution_id で埋める。
        #   朝の 4667 (ブローカー由来: BUY_CREDIT / hold_id=E2026... /
        #   exchange=27 / margin_trade_type=3) と同じ形にする。
        db_side = _to_db_side(side, margin_trade_type=margin_trade_type, hold_id=hold_id or execution_id)
        db_hold_id = str(hold_id or execution_id or "") or None
        # 2026-09-18: 起動時の建玉同期が on_entry_filled を再発火させ、同じ
        #   hold_id の行が増え続けていた (9/18 の 9984 は 11:00 / 11:21 / 11:38 の
        #   3 行、すべて hold_id=E20260918038X5 で実玉は 1 件)。決済は
        #   broker_credit_authoritative で通るが、日次レポートの「未決済 3 件」の
        #   ように記録が実体と合わなくなる。同じ hold_id の未決済行があれば作らない。
        _existing = None
        if db_hold_id and str(os.getenv("ENTRY_FILL_DEDUPE_BY_HOLD_ID", "1")).strip().lower() not in ("0", "false", "no", "off"):
            try:
                _existing = (
                    session.query(Position)
                    .filter(Position.hold_id == db_hold_id)
                    .filter(Position.status == "OPEN")
                    .first()
                )
            except Exception:
                logger.debug("[ENTRY FILL] hold_id 重複チェックに失敗 symbol=%s", symbol, exc_info=True)
                _existing = None
        if _existing is not None:
            logger.info(
                "[ENTRY FILL] 同じ hold_id の未決済行が既にある -> 新規作成しない "
                "symbol=%s hold_id=%s id=%s qty=%s avg_price=%s",
                symbol, db_hold_id, getattr(_existing, "id", None),
                getattr(_existing, "qty", None), getattr(_existing, "avg_price", None),
            )
            pos = _existing
        else:
            pos = Position(
                symbol=symbol,
                symbolname=symbolname,
                side=db_side,
                qty=qty,
                avg_price=price,
                entry_time=now,
                status="OPEN",
                exchange=exchange,
                margin_trade_type=margin_trade_type,
                account_type=account_type,
                hold_id=db_hold_id,
                execution_id=str(execution_id or hold_id or "") or None,
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
            "✅ ENTRY FILLED %s %s qty=%s price=%.2f order_id=%s hold_id=%s exchange=%s margin_trade_type=%s",
            symbol, db_side, qty, price, order_id, db_hold_id, exchange, margin_trade_type,
        )

    except Exception:
        session.rollback()
        logger.exception("❌ on_entry_filled ERROR symbol=%s", symbol)

    finally:
        session.close()
