# ============================================================
# File   : trading/exit/executor.py
# Version: Ver28.0-PRODUCTION-CLOSE-ORDER-CONNECTED
# ------------------------------------------------------------
# ✔ EXIT最終実行層
# ✔ 二重EXIT防止（CLOSING / CLOSED + Thread Lock）
# ✔ NaN / inf 防御
# ✔ PnL計算
# ✔ 返済注文成功後だけ DB / global_data を CLOSED 更新
# ✔ 注文失敗時は status を OPEN に戻す
# ✔ 外部の注文関数を set_exit_order_sender() で注入可能
# ✔ global_data.exit_order_sender があれば自動使用
# ✔ 今回修正:
#   - global_data.open_positions だけでなく GC.positions からも保有を探す
#   - global_data.open_positions が無い/空でも EXIT が止まらない
#   - sender 未注入時は kabuステーションAPIへ信用返済成行を直接送る
#   - BUY建玉 -> SELL返済 / SELL建玉 -> BUY返済
#   - 返済 payload は CashMargin=3 / ClosePositionOrder=4 を使用
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import threading
from typing import Any, Callable, Optional

import configparser

from core.global_context.context import global_context as GC
from global_state import global_data
from database import Session_position
from database.models import Position
from kabu_api.send_order import send_order_common

logger = logging.getLogger(__name__)

# ============================================================
# global lock / injected sender
# ============================================================

_exit_lock = threading.Lock()
_EXIT_ORDER_SENDER: Optional[Callable[..., Any]] = None


# ============================================================
# settings
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


EXIT_REQUIRE_ORDER_SUCCESS = _env_bool("EXIT_REQUIRE_ORDER_SUCCESS", True)
EXIT_DRY_RUN_ORDER_SUCCESS = _env_bool("EXIT_DRY_RUN_ORDER_SUCCESS", False)

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")


# ============================================================
# public hook
# ============================================================

def set_exit_order_sender(sender: Optional[Callable[..., Any]]) -> None:
    global _EXIT_ORDER_SENDER

    if sender is not None and not callable(sender):
        raise TypeError("sender must be callable or None")

    _EXIT_ORDER_SENDER = sender

    logger.info(
        "[EXIT_EXECUTOR] exit order sender set sender=%s",
        getattr(sender, "__name__", type(sender).__name__) if sender else None,
    )


# ============================================================
# safe util
# ============================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
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
        return int(float(v))
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
    if s in {"BUY", "LONG", "2", "現物買", "信用買", "BUY_CREDIT"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "現物売", "信用売", "SELL_CREDIT"}:
        return "SELL"
    return s


def _close_side_from_position_side(side: str) -> str:
    side = _normalize_side(side)
    if side == "BUY":
        return "SELL"
    if side == "SELL":
        return "BUY"
    return ""


def _position_to_dict(pos: Any) -> dict[str, Any]:
    if pos is None:
        return {}
    if isinstance(pos, dict):
        return pos

    out: dict[str, Any] = {}
    for name in [
        "symbol", "side", "qty", "quantity", "avg_price", "entry_price",
        "entry_time", "created_at", "status", "atr", "atr_1min",
        "order_id", "entry_order_id", "exchange",
    ]:
        try:
            if hasattr(pos, name):
                out[name] = getattr(pos, name)
        except Exception:
            pass
    return out


def _snapshot_gc_positions() -> dict[str, Any]:
    try:
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is None:
            return {}

        for method_name in ["snapshot_open", "snapshot_dict", "get_open_positions", "to_dict"]:
            fn = getattr(positions_obj, method_name, None)
            if callable(fn):
                try:
                    ret = fn() or {}
                    if isinstance(ret, dict) and ret:
                        return ret
                except Exception:
                    logger.debug("[EXIT_EXECUTOR] GC.positions.%s failed", method_name, exc_info=True)

        for attr in ["open_positions", "positions"]:
            ret = getattr(positions_obj, attr, None)
            if isinstance(ret, dict) and ret:
                return ret
    except Exception:
        logger.debug("[EXIT_EXECUTOR] GC.positions snapshot failed", exc_info=True)
    return {}


def _ensure_global_open_positions() -> dict[str, Any]:
    try:
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            return positions
        positions = {}
        setattr(global_data, "open_positions", positions)
        return positions
    except Exception:
        return {}


def _get_open_position(symbol: str) -> tuple[Optional[dict[str, Any]], str]:
    """
    execute_exit 用の保有取得。

    以前は global_data.open_positions だけを見ていたため、
    entry 側が GC.positions にだけ保存した場合に EXIT が実行不能だった。
    """
    symbol = _normalize_symbol(symbol)

    # 1. global_data.open_positions
    try:
        gd_positions = _ensure_global_open_positions()
        pos = gd_positions.get(symbol) or gd_positions.get(str(symbol))
        if pos is not None:
            d = _position_to_dict(pos)
            if d:
                gd_positions[symbol] = d
                return d, "global_data.open_positions"
    except Exception:
        logger.debug("[EXIT_EXECUTOR] global_data position lookup failed symbol=%s", symbol, exc_info=True)

    # 2. GC.positions fallback
    try:
        gc_positions = _snapshot_gc_positions()
        pos = gc_positions.get(symbol) or gc_positions.get(str(symbol))
        if pos is not None:
            d = _position_to_dict(pos)
            if d:
                d.setdefault("symbol", symbol)
                # executor 内の後続処理・二重返済防止のため global_data にも同期
                gd_positions = _ensure_global_open_positions()
                gd_positions[symbol] = d
                logger.warning(
                    "[EXIT_EXECUTOR] position recovered from GC.positions symbol=%s",
                    symbol,
                )
                return d, "GC.positions"
    except Exception:
        logger.debug("[EXIT_EXECUTOR] GC position lookup failed symbol=%s", symbol, exc_info=True)

    return None, ""


def _parse_order_result(ret: Any) -> tuple[bool, str, str]:
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


def _get_injected_sender() -> Optional[Callable[..., Any]]:
    if _EXIT_ORDER_SENDER is not None:
        return _EXIT_ORDER_SENDER

    for attr in [
        "exit_order_sender",
        "close_position_sender",
        "send_exit_order",
    ]:
        try:
            fn = getattr(global_data, attr, None)
            if callable(fn):
                return fn
        except Exception:
            pass

    return None


def _build_kabu_close_payload(
    *,
    symbol: str,
    close_side: str,
    qty: float,
    exchange: int = 1,
) -> dict[str, Any]:
    """
    kabuステーション信用返済成行 payload。

    CashMargin:
      2 = 信用新規
      3 = 信用返済

    ClosePositionOrder=4:
      返済建玉指定を省略し、建玉日順などkabuS側ルールで返済。
      個別建玉指定が必要になった場合は ClosePositions を追加する。
    """
    side_num = 1 if close_side == "SELL" else 2

    return {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": int(exchange),
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


def _send_kabu_close_order_builtin(
    *,
    symbol: str,
    side: str,
    close_side: str,
    qty: float,
    exit_price: float,
    reason: str,
    position: dict[str, Any],
) -> tuple[bool, str, str]:
    try:
        exchange = _safe_int(position.get("exchange") or position.get("Exchange"), 1)
        payload = _build_kabu_close_payload(
            symbol=symbol,
            close_side=close_side,
            qty=qty,
            exchange=exchange,
        )

        logger.warning(
            "[EXIT_EXECUTOR] builtin kabu close order send symbol=%s side=%s close_side=%s qty=%s ref_price=%.4f reason=%s payload=%s",
            symbol,
            side,
            close_side,
            int(qty),
            exit_price,
            reason,
            payload,
        )

        ret = send_order_common(payload)
        ok, order_id, message = _parse_order_result(ret)

        if ok:
            logger.info(
                "[EXIT_EXECUTOR] builtin close order accepted symbol=%s order_id=%s message=%s",
                symbol,
                order_id,
                message,
            )
        else:
            logger.error(
                "[EXIT_EXECUTOR] builtin close order rejected symbol=%s message=%s",
                symbol,
                message,
            )

        return ok, order_id, message

    except Exception as e:
        logger.exception("[EXIT_EXECUTOR] builtin close order failed symbol=%s", symbol)
        return False, "", str(e)


def _send_close_order(
    *,
    symbol: str,
    side: str,
    close_side: str,
    qty: float,
    exit_price: float,
    reason: str,
    position: dict[str, Any],
) -> tuple[bool, str, str]:
    if EXIT_DRY_RUN_ORDER_SUCCESS:
        logger.warning(
            "[EXIT_EXECUTOR] DRY_RUN order success enabled. No real close order sent. symbol=%s close_side=%s qty=%.4f price=%.4f reason=%s",
            symbol,
            close_side,
            qty,
            exit_price,
            reason,
        )
        return True, "DRY_RUN", "dry run order success"

    sender = _get_injected_sender()

    if callable(sender):
        try:
            ret = sender(
                symbol=symbol,
                close_side=close_side,
                qty=qty,
                price=exit_price,
                reason=reason,
                position=position,
            )
            ok, order_id, message = _parse_order_result(ret)
            if ok:
                logger.info(
                    "[EXIT_EXECUTOR] injected close order accepted symbol=%s close_side=%s qty=%.4f price=%.4f order_id=%s message=%s",
                    symbol,
                    close_side,
                    qty,
                    exit_price,
                    order_id,
                    message,
                )
            else:
                logger.error(
                    "[EXIT_EXECUTOR] injected close order rejected symbol=%s close_side=%s qty=%.4f price=%.4f message=%s",
                    symbol,
                    close_side,
                    qty,
                    exit_price,
                    message,
                )
            return ok, order_id, message
        except Exception as e:
            logger.exception(
                "[EXIT_EXECUTOR] injected close order sender failed symbol=%s close_side=%s qty=%.4f price=%.4f",
                symbol,
                close_side,
                qty,
                exit_price,
            )
            return False, "", str(e)

    # sender 未接続でもここで止めず、組み込みの信用返済注文を使う。
    logger.warning(
        "[EXIT_EXECUTOR] injected close order sender not connected -> use builtin kabu close order symbol=%s",
        symbol,
    )
    return _send_kabu_close_order_builtin(
        symbol=symbol,
        side=side,
        close_side=close_side,
        qty=qty,
        exit_price=exit_price,
        reason=reason,
        position=position,
    )


def _calculate_pnl(
    *,
    side: str,
    qty: float,
    avg_price: float,
    exit_price: float,
) -> float:
    side = _normalize_side(side)
    if side == "BUY":
        pnl = (exit_price - avg_price) * qty
    elif side == "SELL":
        pnl = (avg_price - exit_price) * qty
    else:
        pnl = 0.0
    return _safe_float(pnl)


def _restore_open_status(symbol: str, pos: dict[str, Any]) -> None:
    try:
        if pos.get("status") == "CLOSING":
            pos["status"] = "OPEN"
            pos["updated_at"] = dt.datetime.now()
    except Exception:
        logger.warning("[EXIT_EXECUTOR] failed to restore OPEN status symbol=%s", symbol, exc_info=True)


def _close_db_position(
    *,
    symbol: str,
    exit_price: float,
    pnl: float,
    reason: str,
    order_id: str,
) -> bool:
    session = Session_position()

    try:
        db_pos = (
            session.query(Position)
            .filter(Position.symbol == symbol)
            .filter(Position.status == "OPEN")
            .first()
        )

        if not db_pos:
            logger.warning("[EXIT_EXECUTOR] DB open position not found symbol=%s", symbol)
            session.rollback()
            return False

        now = dt.datetime.now()
        db_pos.exit_price = exit_price
        db_pos.status = "CLOSED"

        for attr, value in [
            ("exit_time", now),
            ("closed_time", now),
            ("close_time", now),
            ("updated_at", now),
            ("pnl", pnl),
            ("exit_reason", reason),
            ("close_reason", reason),
            ("exit_order_id", order_id),
            ("order_id", order_id),
        ]:
            try:
                if hasattr(db_pos, attr):
                    setattr(db_pos, attr, value)
            except Exception:
                pass

        session.commit()
        logger.info(
            "[EXIT_EXECUTOR] DB position closed symbol=%s exit_price=%.4f pnl=%.4f order_id=%s",
            symbol,
            exit_price,
            pnl,
            order_id,
        )
        return True

    except Exception:
        session.rollback()
        logger.exception("[EXIT_EXECUTOR_DB_ERROR] symbol=%s", symbol)
        return False

    finally:
        session.close()


def _remove_from_memory_positions(symbol: str) -> None:
    try:
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict) and symbol in positions:
            del positions[symbol]
    except Exception:
        logger.warning("[EXIT_EXECUTOR] failed to remove from global_data.open_positions symbol=%s", symbol, exc_info=True)

    try:
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is None:
            return
        for method_name in ["remove", "close", "mark_closed", "delete"]:
            fn = getattr(positions_obj, method_name, None)
            if callable(fn):
                try:
                    fn(symbol)
                    return
                except Exception:
                    pass
        raw = getattr(positions_obj, "open_positions", None)
        if isinstance(raw, dict) and symbol in raw:
            del raw[symbol]
    except Exception:
        logger.debug("[EXIT_EXECUTOR] failed to remove from GC.positions symbol=%s", symbol, exc_info=True)


# ============================================================
# main
# ============================================================

def execute_exit(symbol: str, reason: str, exit_price: float) -> bool:
    symbol = _normalize_symbol(symbol)

    if not symbol:
        logger.warning("[EXIT_EXECUTOR] empty symbol")
        return False

    with _exit_lock:
        pos: Optional[dict[str, Any]] = None

        try:
            pos, pos_source = _get_open_position(symbol)
            if pos is None:
                logger.warning("[EXIT_EXECUTOR] no open position symbol=%s", symbol)
                return False

            status = str(pos.get("status") or "OPEN").upper()
            if status in {"CLOSING", "CLOSED"}:
                logger.debug("[EXIT_EXECUTOR] already closing/closed symbol=%s status=%s", symbol, status)
                return False

            pos["status"] = "CLOSING"
            pos["updated_at"] = dt.datetime.now()

            side = _normalize_side(pos.get("side"))
            close_side = _close_side_from_position_side(side)

            qty = _safe_float(pos.get("qty") or pos.get("quantity"))
            avg_price = _safe_float(pos.get("avg_price") or pos.get("entry_price"))
            exit_price = _safe_float(exit_price)

            if side not in {"BUY", "SELL"}:
                logger.warning("[EXIT_EXECUTOR] invalid side symbol=%s side=%s pos=%s", symbol, side, pos)
                _restore_open_status(symbol, pos)
                return False

            if close_side not in {"BUY", "SELL"}:
                logger.warning("[EXIT_EXECUTOR] invalid close_side symbol=%s side=%s close_side=%s", symbol, side, close_side)
                _restore_open_status(symbol, pos)
                return False

            if qty <= 0 or avg_price <= 0 or exit_price <= 0:
                logger.warning(
                    "[EXIT_EXECUTOR] invalid numeric values symbol=%s qty=%.4f avg=%.4f exit=%.4f pos=%s",
                    symbol,
                    qty,
                    avg_price,
                    exit_price,
                    pos,
                )
                _restore_open_status(symbol, pos)
                return False

            logger.warning(
                "🚪 EXIT REQUEST symbol=%s side=%s close_side=%s qty=%.4f price=%.4f reason=%s pos_source=%s",
                symbol,
                side,
                close_side,
                qty,
                exit_price,
                reason,
                pos_source,
            )

            order_ok, order_id, order_message = _send_close_order(
                symbol=symbol,
                side=side,
                close_side=close_side,
                qty=qty,
                exit_price=exit_price,
                reason=reason,
                position=pos,
            )

            if EXIT_REQUIRE_ORDER_SUCCESS and not order_ok:
                logger.error(
                    "[EXIT_EXECUTOR] exit aborted because close order failed. symbol=%s message=%s",
                    symbol,
                    order_message,
                )
                _restore_open_status(symbol, pos)
                return False

            pnl = _calculate_pnl(side=side, qty=qty, avg_price=avg_price, exit_price=exit_price)

            db_ok = _close_db_position(
                symbol=symbol,
                exit_price=exit_price,
                pnl=pnl,
                reason=reason,
                order_id=order_id,
            )

            if not db_ok:
                logger.error(
                    "[EXIT_EXECUTOR] close order may be accepted but DB close failed. symbol=%s order_id=%s",
                    symbol,
                    order_id,
                )
                pos["status"] = "CLOSING"
                pos["exit_order_id"] = order_id
                pos["exit_order_message"] = order_message
                pos["updated_at"] = dt.datetime.now()
                return False

            pos["exit_price"] = exit_price
            pos["pnl"] = pnl
            pos["status"] = "CLOSED"
            pos["exit_time"] = dt.datetime.now()
            pos["exit_reason"] = reason
            pos["exit_order_id"] = order_id
            pos["exit_order_message"] = order_message
            pos["updated_at"] = dt.datetime.now()

            logger.warning(
                "✅ EXIT COMPLETE symbol=%s pnl=%.4f reason=%s order_id=%s",
                symbol,
                pnl,
                reason,
                order_id,
            )

            _remove_from_memory_positions(symbol)
            return True

        except Exception:
            logger.exception("[EXIT_EXECUTOR_FATAL] symbol=%s", symbol)
            if pos is not None:
                _restore_open_status(symbol, pos)
            return False


__all__ = [
    "set_exit_order_sender",
    "execute_exit",
]
