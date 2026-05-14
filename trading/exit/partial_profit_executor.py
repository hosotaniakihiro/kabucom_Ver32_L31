# ============================================================
# File   : trading/exit/partial_profit_executor.py
# Version: V1.0-PARTIAL-PROFIT-TAKE-CREDIT-CLOSE
# ------------------------------------------------------------
# 【概要】
#   利大用の「一部利確」実行層。
#
# 【方針】
#   - +0.20% 等の小さな利益で半分だけ信用返済する。
#   - 残り建玉は OPEN のまま残し、通常のトレーリングEXITで伸ばす。
#   - 100株単位で返済数量を丸める。
#   - 1単元しかない場合は一部利確しない。
#
# 【注意】
#   - 全返済は trading.exit.executor.execute_exit が担当。
#   - このファイルは partial close 専用。
# ============================================================

from __future__ import annotations

import configparser
import datetime as dt
import logging
import math
import os
from typing import Any, Dict, Optional, Tuple

from core.global_context.context import global_context as GC
from global_state import global_data
from database import Session_position
from database.models import Position
from kabu_api.send_order import send_order_common

logger = logging.getLogger(__name__)

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")

PARTIAL_PROFIT_MIN_LOT = int(float(os.getenv("PARTIAL_PROFIT_MIN_LOT", "100")))
PARTIAL_PROFIT_REQUIRE_ORDER_SUCCESS = str(os.getenv("PARTIAL_PROFIT_REQUIRE_ORDER_SUCCESS", "1")).lower() not in {
    "0", "false", "no", "off",
}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        x = int(float(v))
        return x
    except Exception:
        return int(default)


def _normalize_symbol(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _normalize_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "信用買", "買", "買建", "BUY_CREDIT"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "信用売", "売", "売建", "SELL_CREDIT"}:
        return "SELL"
    return s


def _close_side_from_position_side(side: str) -> str:
    side = _normalize_side(side)
    if side == "BUY":
        return "SELL"
    if side == "SELL":
        return "BUY"
    return ""


def _parse_order_result(ret: Any) -> Tuple[bool, str, str]:
    if ret is True:
        return True, "", "true"
    if ret is False or ret is None:
        return False, "", str(ret)

    if isinstance(ret, dict):
        ok = bool(
            ret.get("ok")
            or ret.get("success")
            or ret.get("result")
            or ret.get("accepted")
            or ret.get("OrderId")
            or ret.get("order_id")
        )
        order_id = str(
            ret.get("order_id")
            or ret.get("orderId")
            or ret.get("OrderId")
            or ret.get("id")
            or ""
        )
        message = str(
            ret.get("message")
            or ret.get("msg")
            or ret.get("error")
            or ret.get("reason")
            or ret
        )
        return ok, order_id, message

    ok_attr = getattr(ret, "ok", None)
    success_attr = getattr(ret, "success", None)
    accepted_attr = getattr(ret, "accepted", None)
    if ok_attr is not None or success_attr is not None or accepted_attr is not None:
        ok = bool(ok_attr or success_attr or accepted_attr)
        order_id = str(
            getattr(ret, "order_id", "")
            or getattr(ret, "orderId", "")
            or getattr(ret, "OrderId", "")
            or getattr(ret, "id", "")
            or ""
        )
        return ok, order_id, str(ret)

    return False, "", str(ret)


def _round_down_lot(qty: float, lot: int = PARTIAL_PROFIT_MIN_LOT) -> int:
    lot = max(1, int(lot or 1))
    return int(float(qty) // lot * lot)


def _calc_partial_qty(total_qty: int, ratio: float) -> int:
    """半分利確数量。最低1単元は残す。"""
    lot = max(1, int(PARTIAL_PROFIT_MIN_LOT or 100))
    total_qty = _round_down_lot(total_qty, lot)
    if total_qty < lot * 2:
        return 0

    ratio = max(0.0, min(1.0, float(ratio or 0.5)))
    partial_qty = _round_down_lot(total_qty * ratio, lot)

    if partial_qty < lot:
        partial_qty = lot

    # 全部返済にならないよう最低1単元残す。
    if partial_qty >= total_qty:
        partial_qty = total_qty - lot

    return _round_down_lot(partial_qty, lot)


def _build_close_payload(*, symbol: str, close_side: str, qty: int, exchange: int = 1) -> Dict[str, Any]:
    side_num = 1 if close_side == "SELL" else 2
    return {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": int(exchange or 1),
        "SecurityType": 1,
        "Side": int(side_num),
        "CashMargin": 3,
        "MarginTradeType": 1,
        "DelivType": 2,
        "AccountType": 4,
        "Qty": int(qty),
        "ClosePositionOrder": 4,
        "FrontOrderType": 10,
        "Price": 0,
        "ExpireDay": 0,
    }


def _update_memory_position(symbol: str, remaining_qty: int, *, order_id: str, reason: str, price: float) -> None:
    try:
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            pos = positions.get(symbol) or positions.get(str(symbol))
            if isinstance(pos, dict):
                pos["qty"] = remaining_qty
                pos["quantity"] = remaining_qty
                pos["partial_profit_taken"] = True
                pos["partial_profit_order_id"] = order_id
                pos["partial_profit_reason"] = reason
                pos["partial_profit_price"] = price
                pos["updated_at"] = dt.datetime.now()
    except Exception:
        logger.debug("[PARTIAL PROFIT] global_data memory update failed symbol=%s", symbol, exc_info=True)

    try:
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is not None:
            raw = getattr(positions_obj, "open_positions", None)
            if isinstance(raw, dict):
                pos = raw.get(symbol) or raw.get(str(symbol))
                if isinstance(pos, dict):
                    pos["qty"] = remaining_qty
                    pos["quantity"] = remaining_qty
                    pos["partial_profit_taken"] = True
                    pos["partial_profit_order_id"] = order_id
                    pos["partial_profit_reason"] = reason
                    pos["partial_profit_price"] = price
                    pos["updated_at"] = dt.datetime.now()
    except Exception:
        logger.debug("[PARTIAL PROFIT] GC memory update failed symbol=%s", symbol, exc_info=True)


def _update_db_position_qty(symbol: str, remaining_qty: int, *, order_id: str, reason: str, price: float) -> bool:
    session = Session_position()
    try:
        db_pos = (
            session.query(Position)
            .filter(Position.symbol == symbol)
            .filter(Position.status == "OPEN")
            .first()
        )
        if not db_pos:
            logger.warning("[PARTIAL PROFIT] DB open position not found symbol=%s", symbol)
            session.rollback()
            return False

        db_pos.qty = int(remaining_qty)
        db_pos.updated_at = dt.datetime.now()

        # schema に追加属性がある場合だけ診断情報を入れる。
        for attr, value in [
            ("partial_profit_order_id", order_id),
            ("partial_profit_reason", reason),
            ("partial_profit_price", price),
            ("partial_profit_time", dt.datetime.now()),
        ]:
            try:
                if hasattr(db_pos, attr):
                    setattr(db_pos, attr, value)
            except Exception:
                pass

        session.commit()
        logger.warning(
            "[PARTIAL PROFIT] DB qty reduced symbol=%s remaining_qty=%s order_id=%s reason=%s",
            symbol,
            remaining_qty,
            order_id,
            reason,
        )
        return True

    except Exception:
        session.rollback()
        logger.exception("[PARTIAL PROFIT] DB qty update failed symbol=%s", symbol)
        return False
    finally:
        session.close()


def execute_partial_profit(
    *,
    symbol: str,
    pos: Dict[str, Any],
    reason: str,
    exit_price: float,
    ratio: float = 0.5,
) -> bool:
    """
    信用建玉を一部返済する。

    Returns
    -------
    bool
        True: 一部返済注文が成功し、残数量へ更新できた。
        False: 何もしない、または注文/更新失敗。
    """
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return False

    if not isinstance(pos, dict):
        logger.warning("[PARTIAL PROFIT] skip invalid pos symbol=%s pos_type=%s", symbol, type(pos))
        return False

    side = _normalize_side(pos.get("side") or pos.get("Side"))
    close_side = _close_side_from_position_side(side)
    total_qty = _safe_int(pos.get("qty") or pos.get("quantity"), 0)
    exit_price = _safe_float(exit_price, 0.0)

    if side not in {"BUY", "SELL"} or close_side not in {"BUY", "SELL"}:
        logger.warning("[PARTIAL PROFIT] skip invalid side symbol=%s side=%s", symbol, side)
        return False

    if total_qty <= 0 or exit_price <= 0:
        logger.warning("[PARTIAL PROFIT] skip invalid qty/price symbol=%s qty=%s price=%s", symbol, total_qty, exit_price)
        return False

    partial_qty = _calc_partial_qty(total_qty, ratio)
    if partial_qty <= 0:
        logger.warning(
            "[PARTIAL PROFIT] skip qty too small symbol=%s total_qty=%s ratio=%.3f min_lot=%s",
            symbol,
            total_qty,
            ratio,
            PARTIAL_PROFIT_MIN_LOT,
        )
        return False

    remaining_qty = int(total_qty - partial_qty)
    if remaining_qty <= 0:
        logger.warning("[PARTIAL PROFIT] skip no remaining qty symbol=%s total=%s partial=%s", symbol, total_qty, partial_qty)
        return False

    exchange = _safe_int(pos.get("exchange") or pos.get("Exchange"), 1)
    payload = _build_close_payload(symbol=symbol, close_side=close_side, qty=partial_qty, exchange=exchange)

    logger.warning(
        "[PARTIAL PROFIT] order send symbol=%s side=%s close_side=%s total_qty=%s partial_qty=%s remaining_qty=%s price=%.4f reason=%s payload=%s",
        symbol,
        side,
        close_side,
        total_qty,
        partial_qty,
        remaining_qty,
        exit_price,
        reason,
        payload,
    )

    try:
        ret = send_order_common(payload)
        ok, order_id, message = _parse_order_result(ret)
    except Exception as e:
        logger.exception("[PARTIAL PROFIT] order send failed symbol=%s", symbol)
        ok, order_id, message = False, "", str(e)

    if PARTIAL_PROFIT_REQUIRE_ORDER_SUCCESS and not ok:
        logger.error("[PARTIAL PROFIT] order rejected symbol=%s message=%s", symbol, message)
        return False

    db_ok = _update_db_position_qty(symbol, remaining_qty, order_id=order_id, reason=reason, price=exit_price)
    _update_memory_position(symbol, remaining_qty, order_id=order_id, reason=reason, price=exit_price)

    if not db_ok:
        logger.error(
            "[PARTIAL PROFIT] order may be accepted but DB qty update failed symbol=%s order_id=%s remaining_qty=%s",
            symbol,
            order_id,
            remaining_qty,
        )
        return False

    logger.warning(
        "[PARTIAL PROFIT] COMPLETE symbol=%s partial_qty=%s remaining_qty=%s order_id=%s reason=%s",
        symbol,
        partial_qty,
        remaining_qty,
        order_id,
        reason,
    )
    return True


__all__ = ["execute_partial_profit"]
