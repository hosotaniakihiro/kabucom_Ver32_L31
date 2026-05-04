# ============================================================
# File   : trading/exit/executor.py
# Version: Ver27.0-PRODUCTION-SAFE-ORDER-GATED-FINAL
# ------------------------------------------------------------
# ✔ EXIT最終実行層
# ✔ 二重EXIT防止（CLOSING / CLOSED + Thread Lock）
# ✔ NaN / inf 防御
# ✔ PnL計算
# ✔ 返済注文成功後だけ DB / global_data を CLOSED 更新
# ✔ 返済注文API未接続なら CLOSED にしない
# ✔ 注文失敗時は status を OPEN に戻す
# ✔ 外部の注文関数を set_exit_order_sender() で注入可能
# ✔ global_data.exit_order_sender があれば自動使用
# ✔ 本番安全側 fail-closed
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import threading
from typing import Any, Callable, Optional

from global_state import global_data
from database import Session_position
from database.models import Position

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


# 実注文APIが未接続のまま DB CLOSED になる事故を防ぐ
EXIT_REQUIRE_ORDER_SUCCESS = _env_bool("EXIT_REQUIRE_ORDER_SUCCESS", True)

# テスト用。通常は False。
# True にすると注文APIなしでも成功扱いにできるが、本番では絶対に使わない。
EXIT_DRY_RUN_ORDER_SUCCESS = _env_bool("EXIT_DRY_RUN_ORDER_SUCCESS", False)


# ============================================================
# public hook
# ============================================================

def set_exit_order_sender(sender: Optional[Callable[..., Any]]) -> None:
    """
    EXIT返済注文送信用の関数を外部から注入する。

    sender の想定シグネチャ例:
        sender(
            symbol=symbol,
            close_side=close_side,
            qty=qty,
            price=exit_price,
            reason=reason,
            position=pos,
        )

    戻り値:
        True
        または
        {"ok": True, "order_id": "..."}
    """
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


def _normalize_symbol(v: Any) -> str:
    if v is None:
        return ""

    s = str(v).strip()

    if s.endswith(".0"):
        s = s[:-2]

    return s


def _normalize_side(v: Any) -> str:
    s = str(v or "").strip().upper()

    if s in {"BUY", "LONG", "2", "現物買", "信用買"}:
        return "BUY"

    if s in {"SELL", "SHORT", "1", "現物売", "信用売"}:
        return "SELL"

    return s


def _close_side_from_position_side(side: str) -> str:
    """
    建玉sideから返済注文sideを決める。

    BUY建玉  -> SELL返済
    SELL建玉 -> BUY返済
    """
    side = _normalize_side(side)

    if side == "BUY":
        return "SELL"

    if side == "SELL":
        return "BUY"

    return ""


def _parse_order_result(ret: Any) -> tuple[bool, str, str]:
    """
    注文関数の戻り値を標準化する。

    Returns:
        ok, order_id, message
    """
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

    # オブジェクト型にも一応対応
    ok_attr = getattr(ret, "ok", None)
    success_attr = getattr(ret, "success", None)
    accepted_attr = getattr(ret, "accepted", None)

    if ok_attr is not None or success_attr is not None or accepted_attr is not None:
        ok = bool(ok_attr or success_attr or accepted_attr)
        order_id = str(
            getattr(ret, "order_id", "")
            or getattr(ret, "orderId", "")
            or getattr(ret, "id", "")
            or ""
        )
        return ok, order_id, str(ret)

    return False, "", str(ret)


def _get_injected_sender() -> Optional[Callable[..., Any]]:
    """
    注文送信関数を探す。

    優先順位:
      1. set_exit_order_sender() で注入された関数
      2. global_data.exit_order_sender
      3. global_data.close_position_sender
    """
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
    """
    実返済注文を送る。

    重要:
      - 注文APIが未接続なら False
      - False の場合、DB / global_data は CLOSED にしない
    """
    if EXIT_DRY_RUN_ORDER_SUCCESS:
        logger.warning(
            "[EXIT_EXECUTOR] DRY_RUN order success enabled. "
            "No real close order sent. symbol=%s close_side=%s qty=%.4f price=%.4f reason=%s",
            symbol,
            close_side,
            qty,
            exit_price,
            reason,
        )
        return True, "DRY_RUN", "dry run order success"

    sender = _get_injected_sender()

    if not callable(sender):
        logger.error(
            "[EXIT_EXECUTOR] close order sender not connected. "
            "DB will NOT be closed. symbol=%s side=%s close_side=%s qty=%.4f price=%.4f reason=%s",
            symbol,
            side,
            close_side,
            qty,
            exit_price,
            reason,
        )
        return False, "", "order sender not connected"

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
                "[EXIT_EXECUTOR] close order accepted symbol=%s close_side=%s qty=%.4f "
                "price=%.4f order_id=%s message=%s",
                symbol,
                close_side,
                qty,
                exit_price,
                order_id,
                message,
            )
        else:
            logger.error(
                "[EXIT_EXECUTOR] close order rejected symbol=%s close_side=%s qty=%.4f "
                "price=%.4f message=%s",
                symbol,
                close_side,
                qty,
                exit_price,
                message,
            )

        return ok, order_id, message

    except Exception as e:
        logger.exception(
            "[EXIT_EXECUTOR] close order sender failed symbol=%s close_side=%s qty=%.4f price=%.4f",
            symbol,
            close_side,
            qty,
            exit_price,
        )
        return False, "", str(e)


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
    """
    注文失敗時にメモリ状態を OPEN に戻す。
    """
    try:
        if pos.get("status") == "CLOSING":
            pos["status"] = "OPEN"
            pos["updated_at"] = dt.datetime.now()
    except Exception:
        logger.warning(
            "[EXIT_EXECUTOR] failed to restore OPEN status symbol=%s",
            symbol,
            exc_info=True,
        )


def _close_db_position(
    *,
    symbol: str,
    exit_price: float,
    pnl: float,
    reason: str,
    order_id: str,
) -> bool:
    """
    DB上の OPEN position を CLOSED にする。

    注意:
      - これは注文成功後だけ呼ぶこと
    """
    session = Session_position()

    try:
        db_pos = (
            session.query(Position)
            .filter(Position.symbol == symbol)
            .filter(Position.status == "OPEN")
            .first()
        )

        if not db_pos:
            logger.warning(
                "[EXIT_EXECUTOR] DB open position not found symbol=%s",
                symbol,
            )
            session.rollback()
            return False

        now = dt.datetime.now()

        db_pos.exit_price = exit_price
        db_pos.status = "CLOSED"

        # 既存モデル差異を吸収
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


# ============================================================
# main
# ============================================================

def execute_exit(
    symbol: str,
    reason: str,
    exit_price: float,
) -> bool:
    """
    EXITを実行する最終関数。

    return:
        True  = 返済注文成功 + DB/global_data CLOSED 更新完了
        False = 実行されなかった、または注文/DB更新失敗

    重要:
      Ver27.0 では、注文成功前に CLOSED にしない。
    """
    symbol = _normalize_symbol(symbol)

    if not symbol:
        logger.warning("[EXIT_EXECUTOR] empty symbol")
        return False

    with _exit_lock:
        pos: Optional[dict[str, Any]] = None

        try:
            positions = getattr(global_data, "open_positions", None)

            if positions is None:
                logger.warning("[EXIT_EXECUTOR] global_data.open_positions missing")
                return False

            # ----------------------------------------------------
            # ポジション存在確認
            # ----------------------------------------------------
            if symbol not in positions:
                logger.debug("[EXIT_EXECUTOR] no open position %s", symbol)
                return False

            pos = positions[symbol]

            if not isinstance(pos, dict):
                logger.warning(
                    "[EXIT_EXECUTOR] invalid position type symbol=%s type=%s",
                    symbol,
                    type(pos).__name__,
                )
                return False

            status = str(pos.get("status") or "OPEN").upper()

            # ----------------------------------------------------
            # 二重EXIT防止
            # ----------------------------------------------------
            if status in {"CLOSING", "CLOSED"}:
                logger.debug(
                    "[EXIT_EXECUTOR] already closing/closed symbol=%s status=%s",
                    symbol,
                    status,
                )
                return False

            pos["status"] = "CLOSING"
            pos["updated_at"] = dt.datetime.now()

            # ----------------------------------------------------
            # 安全値取得
            # ----------------------------------------------------
            side = _normalize_side(pos.get("side"))
            close_side = _close_side_from_position_side(side)

            qty = _safe_float(pos.get("qty"))
            avg_price = _safe_float(pos.get("avg_price"))
            exit_price = _safe_float(exit_price)

            if side not in {"BUY", "SELL"}:
                logger.warning(
                    "[EXIT_EXECUTOR] invalid side symbol=%s side=%s",
                    symbol,
                    side,
                )
                _restore_open_status(symbol, pos)
                return False

            if close_side not in {"BUY", "SELL"}:
                logger.warning(
                    "[EXIT_EXECUTOR] invalid close_side symbol=%s side=%s close_side=%s",
                    symbol,
                    side,
                    close_side,
                )
                _restore_open_status(symbol, pos)
                return False

            if qty <= 0 or avg_price <= 0 or exit_price <= 0:
                logger.warning(
                    "[EXIT_EXECUTOR] invalid numeric values symbol=%s qty=%.4f avg=%.4f exit=%.4f",
                    symbol,
                    qty,
                    avg_price,
                    exit_price,
                )
                _restore_open_status(symbol, pos)
                return False

            logger.info(
                "🚪 EXIT REQUEST symbol=%s side=%s close_side=%s qty=%.4f price=%.4f reason=%s",
                symbol,
                side,
                close_side,
                qty,
                exit_price,
                reason,
            )

            # ----------------------------------------------------
            # 実返済注文
            # ----------------------------------------------------
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
                    "[EXIT_EXECUTOR] exit aborted because close order failed. "
                    "symbol=%s message=%s",
                    symbol,
                    order_message,
                )
                _restore_open_status(symbol, pos)
                return False

            # ----------------------------------------------------
            # PnL計算
            # ----------------------------------------------------
            pnl = _calculate_pnl(
                side=side,
                qty=qty,
                avg_price=avg_price,
                exit_price=exit_price,
            )

            # ----------------------------------------------------
            # DB同期更新
            # 注文成功後だけ CLOSED にする
            # ----------------------------------------------------
            db_ok = _close_db_position(
                symbol=symbol,
                exit_price=exit_price,
                pnl=pnl,
                reason=reason,
                order_id=order_id,
            )

            if not db_ok:
                logger.error(
                    "[EXIT_EXECUTOR] close order may be accepted but DB close failed. "
                    "symbol=%s order_id=%s",
                    symbol,
                    order_id,
                )

                # 注文が成功している可能性があるため、ここで OPEN に戻すと二重返済の危険がある。
                # CLOSING のまま残して手動確認させる。
                pos["status"] = "CLOSING"
                pos["exit_order_id"] = order_id
                pos["exit_order_message"] = order_message
                pos["updated_at"] = dt.datetime.now()
                return False

            # ----------------------------------------------------
            # global_data 更新
            # ----------------------------------------------------
            pos["exit_price"] = exit_price
            pos["pnl"] = pnl
            pos["status"] = "CLOSED"
            pos["exit_time"] = dt.datetime.now()
            pos["exit_reason"] = reason
            pos["exit_order_id"] = order_id
            pos["exit_order_message"] = order_message
            pos["updated_at"] = dt.datetime.now()

            logger.info(
                "✅ EXIT COMPLETE symbol=%s pnl=%.4f reason=%s order_id=%s",
                symbol,
                pnl,
                reason,
                order_id,
            )

            # open_positions から削除
            try:
                if symbol in positions:
                    del positions[symbol]
            except Exception:
                logger.warning(
                    "[EXIT_EXECUTOR] failed to remove from open_positions symbol=%s",
                    symbol,
                    exc_info=True,
                )

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