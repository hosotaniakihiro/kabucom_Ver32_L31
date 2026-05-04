# ============================================================
# File   : trading/exit/order_sender.py
# Version: Ver1.0-PRODUCTION-KABU-CLOSE-ADAPTER
# ------------------------------------------------------------
# Purpose:
#   trading.exit.executor.set_exit_order_sender() に接続する
#   kabu Station 返済注文アダプタ。
#
# Flow:
#   executor.execute_exit()
#     ↓
#   _send_close_order()
#     ↓
#   send_kabu_close_order()
#     ↓
#   kabu_api.close.process_exit()
#
# Important:
#   - executor.py 側は「注文成功後だけ CLOSED 更新」する
#   - このファイルは注文送信だけ担当
#   - DB更新は executor.py に任せる
# ============================================================

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
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


def _is_credit_position(position: dict[str, Any]) -> bool:
    """
    信用建玉かどうかを推定する。

    優先:
      - side が BUY_CREDIT / SELL_CREDIT
      - hold_id がある
      - margin_trade_type がある
    """
    side_raw = str(position.get("side") or "").strip().upper()

    if side_raw in {"BUY_CREDIT", "SELL_CREDIT"}:
        return True

    hold_id = position.get("hold_id") or position.get("HoldID") or position.get("execution_id")
    if hold_id:
        return True

    margin_trade_type = position.get("margin_trade_type")
    if margin_trade_type not in (None, "", 0, "0"):
        return True

    cash_margin = position.get("cash_margin") or position.get("CashMargin")
    if str(cash_margin).strip() in {"2", "3"}:
        return True

    return False


def _to_process_exit_side(position: dict[str, Any]) -> str:
    """
    kabu_api.close.process_exit() が期待する side に変換する。

    process_exit 側:
      BUY_CREDIT  -> 信用買い建玉の売り返済
      SELL_CREDIT -> 信用売り建玉の買い戻し
      BUY         -> 現物買いの売り
      SELL        -> 現物売りの買い
    """
    side_raw = str(position.get("side") or "").strip().upper()

    if side_raw in {"BUY_CREDIT", "SELL_CREDIT"}:
        return side_raw

    side = _normalize_side(side_raw)

    if _is_credit_position(position):
        if side == "BUY":
            return "BUY_CREDIT"
        if side == "SELL":
            return "SELL_CREDIT"

    if side == "BUY":
        return "BUY"

    if side == "SELL":
        return "SELL"

    return side_raw


def _build_position_object(
    *,
    symbol: str,
    qty: float,
    price: float,
    position: dict[str, Any],
) -> SimpleNamespace:
    """
    kabu_api.close.process_exit() に渡す簡易 Position オブジェクトを作る。

    process_exit() は SQLAlchemy Position 型を型ヒントにしているが、
    実際には attribute access だけなので SimpleNamespace で足りる。
    """
    return SimpleNamespace(
        symbol=_normalize_symbol(symbol),
        symbolname=position.get("symbolname") or position.get("name") or "",
        side=_to_process_exit_side(position),

        qty=_safe_int(qty, 0),
        avg_price=_safe_float(position.get("avg_price"), 0.0),
        price=_safe_float(price, 0.0),
        exit_price=_safe_float(price, 0.0),

        hold_id=(
            position.get("hold_id")
            or position.get("HoldID")
            or position.get("execution_id")
            or position.get("ExecutionID")
            or ""
        ),

        exchange=_safe_int(position.get("exchange"), 1),
        margin_trade_type=_safe_int(position.get("margin_trade_type"), 1),
        account_type=_safe_int(position.get("account_type"), 4),

        status=position.get("status") or "OPEN",
        entry_time=position.get("entry_time"),
        exit_time=position.get("exit_time"),
        created_at=position.get("created_at"),
        updated_at=position.get("updated_at"),
    )


def _parse_process_exit_result(ret: Any) -> dict[str, Any]:
    """
    close.py process_exit() の戻り値を executor.py 用に正規化する。
    """
    if ret is None or ret is False:
        return {
            "ok": False,
            "order_id": "",
            "message": str(ret),
        }

    if isinstance(ret, str):
        return {
            "ok": bool(ret),
            "order_id": ret,
            "message": "string order id",
        }

    if isinstance(ret, dict):
        order_id = (
            ret.get("order_id")
            or ret.get("OrderId")
            or ret.get("orderId")
            or ret.get("id")
            or ""
        )

        return {
            "ok": bool(order_id or ret.get("ok") or ret.get("success")),
            "order_id": str(order_id),
            "message": str(ret),
            "raw": ret,
            "exec_price": ret.get("exec_price"),
            "exec_qty": ret.get("exec_qty"),
            "exec_time": ret.get("exec_time"),
        }

    return {
        "ok": False,
        "order_id": "",
        "message": str(ret),
    }


# ============================================================
# main sender
# ============================================================

def send_kabu_close_order(
    *,
    symbol: str,
    close_side: str,
    qty: float,
    price: float,
    reason: str,
    position: dict[str, Any],
) -> dict[str, Any]:
    """
    executor.py から呼ばれる返済注文送信関数。

    Args:
        symbol:
            銘柄コード
        close_side:
            executor 側が計算した返済方向。ここでは参考ログ用。
        qty:
            数量
        price:
            exit_price。close.py 側は成行なので価格は主に記録用。
        reason:
            exit 理由
        position:
            global_data.open_positions[symbol] の dict

    Returns:
        {"ok": True, "order_id": "..."} 形式
    """
    symbol = _normalize_symbol(symbol)

    if not symbol:
        return {"ok": False, "order_id": "", "message": "empty symbol"}

    if not isinstance(position, dict):
        return {
            "ok": False,
            "order_id": "",
            "message": f"invalid position type: {type(position).__name__}",
        }

    qty_i = _safe_int(qty, 0)

    if qty_i <= 0:
        return {
            "ok": False,
            "order_id": "",
            "message": f"invalid qty: {qty}",
        }

    pos_obj = _build_position_object(
        symbol=symbol,
        qty=qty_i,
        price=price,
        position=position,
    )

    logger.info(
        "[EXIT ORDER SENDER] send close order symbol=%s side=%s close_side=%s "
        "qty=%s hold_id=%s exchange=%s margin_trade_type=%s account_type=%s reason=%s",
        pos_obj.symbol,
        pos_obj.side,
        close_side,
        pos_obj.qty,
        pos_obj.hold_id,
        pos_obj.exchange,
        pos_obj.margin_trade_type,
        pos_obj.account_type,
        reason,
    )

    try:
        # まず一般的な配置
        try:
            from kabu_api.close import process_exit
        except Exception:
            # 既存プロジェクトで close.py が直接 import path にある場合の保険
            from close import process_exit  # type: ignore

        ret = process_exit(pos_obj, exit_price=_safe_float(price, 0.0), reason=reason)

        parsed = _parse_process_exit_result(ret)

        if parsed.get("ok"):
            logger.info(
                "[EXIT ORDER SENDER] close order accepted symbol=%s order_id=%s result=%s",
                symbol,
                parsed.get("order_id"),
                parsed,
            )
        else:
            logger.error(
                "[EXIT ORDER SENDER] close order failed symbol=%s result=%s",
                symbol,
                parsed,
            )

        return parsed

    except Exception as e:
        logger.exception(
            "[EXIT ORDER SENDER] close order exception symbol=%s",
            symbol,
        )
        return {
            "ok": False,
            "order_id": "",
            "message": str(e),
        }


def install_exit_order_sender() -> bool:
    """
    executor.py に返済注文senderを登録する。
    起動時に1回呼ぶ。
    """
    try:
        from trading.exit.executor import set_exit_order_sender

        set_exit_order_sender(send_kabu_close_order)

        logger.info("[EXIT ORDER SENDER] installed send_kabu_close_order")
        return True

    except Exception:
        logger.exception("[EXIT ORDER SENDER] install failed")
        return False


__all__ = [
    "send_kabu_close_order",
    "install_exit_order_sender",
]