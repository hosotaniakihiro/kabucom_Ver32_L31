# ============================================================
# File   : core/startup/exit_unfilled_reprice_runtime_patch.py
# Version: V1-EXIT-UNFILLED-REPRICE-ONCE
# ------------------------------------------------------------
# 返済指値が未約定で残った場合、短時間で取消し、建玉をOPENへ戻して
# execute_exit() をもう一度呼ぶ。2回目も未約定なら成行fallbackを許容。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)
_INSTALLED = False
_THREAD_STARTED = False
_FIRST_SEEN: Dict[str, float] = {}
_CANCEL_SENT: Dict[str, float] = {}
_RETRY_ROUNDS: Dict[str, int] = {}
_LOCK = threading.RLock()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(float(v)) if v not in (None, "") else int(default)
    except Exception:
        return int(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v)) if v not in (None, "") else int(default)
    except Exception:
        return int(default)


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _remaining(order: Dict[str, Any]) -> tuple[float, float, float]:
    qty = _f(order.get("OrderQty", order.get("Qty", 0)), 0.0)
    cum = _f(order.get("CumQty", order.get("ContractedQty", 0)), 0.0)
    leaves = _f(order.get("LeavesQty", 0), 0.0)
    if leaves <= 0 and qty > 0:
        leaves = max(qty - cum, 0.0)
    if qty <= 0 and leaves > 0:
        qty = leaves + cum
    return qty, cum, leaves


def _order_elapsed(order_id: str, order: Dict[str, Any], now_ts: float) -> float:
    for key in ("OrderTime", "RecvTime", "ReceivedTime", "RegisterTime", "UpdateTime"):
        raw = order.get(key)
        if not raw:
            continue
        try:
            s = str(raw).strip().replace("T", " ")
            if "+" in s:
                s = s.split("+", 1)[0]
            if s.endswith("Z"):
                s = s[:-1]
            t = dt.datetime.fromisoformat(s)
            return max(0.0, (dt.datetime.now() - t).total_seconds())
        except Exception:
            pass
    if order_id not in _FIRST_SEEN:
        _FIRST_SEEN[order_id] = now_ts
        logger.warning("[EXIT UNFILLED REPRICE] first_seen order_id=%s symbol=%s", order_id, order.get("Symbol"))
    return max(0.0, now_ts - _FIRST_SEEN[order_id])


def _is_close_limit_order(order: Dict[str, Any], cancelable_states: set[int]) -> bool:
    state = _i(order.get("State"), 0)
    price = _f(order.get("Price"), 0.0)
    front = _i(order.get("FrontOrderType"), 0)
    cash_margin = _i(order.get("CashMargin"), 0)
    qty, cum, leaves = _remaining(order)
    is_limit = front == 20 or (front != 10 and price > 0)
    is_open = leaves > 0 or (qty > 0 and cum < qty)
    is_close = cash_margin == 3
    return bool(is_close and is_limit and is_open and state in cancelable_states)


def _restore_position_open(symbol: str) -> None:
    symbol = _sym(symbol)
    try:
        from database import Session_position
        from database.models import Position
        session = Session_position()
        try:
            row = (
                session.query(Position)
                .filter(Position.symbol == symbol)
                .filter(Position.status == "CLOSING")
                .first()
            )
            if row:
                row.status = "OPEN"
                if hasattr(row, "updated_at"):
                    row.updated_at = dt.datetime.now()
                session.commit()
                logger.warning("[EXIT UNFILLED REPRICE] DB restored OPEN symbol=%s", symbol)
            else:
                session.rollback()
        finally:
            session.close()
    except Exception:
        logger.exception("[EXIT UNFILLED REPRICE] DB restore OPEN failed symbol=%s", symbol)

    try:
        from global_state import global_data
        pos = getattr(global_data, "open_positions", None)
        if isinstance(pos, dict):
            p = pos.get(symbol)
            if isinstance(p, dict):
                p["status"] = "OPEN"
                p["updated_at"] = dt.datetime.now()
    except Exception:
        logger.debug("[EXIT UNFILLED REPRICE] memory restore skipped", exc_info=True)


def _retry_exit(symbol: str, order: Dict[str, Any]) -> None:
    symbol = _sym(symbol)
    if not symbol:
        return
    with _LOCK:
        r = _RETRY_ROUNDS.get(symbol, 0)
        max_rounds = _env_int("EXIT_UNFILLED_REPRICE_MAX_ROUNDS", 1)
        if r >= max_rounds:
            if _env_bool("EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL", True):
                os.environ["EXIT_REST_FULL_BOARD_ENABLED"] = "0"
                os.environ["EXIT_LIMIT_BOARD_TOUCH_ENABLED"] = "0"
                logger.warning("[EXIT UNFILLED REPRICE] final retry will use MARKET fallback symbol=%s round=%s", symbol, r)
            else:
                logger.warning("[EXIT UNFILLED REPRICE] max retry reached symbol=%s round=%s", symbol, r)
                return
        _RETRY_ROUNDS[symbol] = r + 1

    try:
        from trading.exit.executor import execute_exit
        ref_price = _f(order.get("Price"), 0.0)
        reason = f"unfilled_exit_reprice_round_{_RETRY_ROUNDS.get(symbol, 0)}"
        logger.warning("[EXIT UNFILLED REPRICE] retry execute_exit symbol=%s ref_price=%s reason=%s", symbol, ref_price, reason)
        execute_exit(symbol, reason=reason, exit_price=ref_price if ref_price > 0 else 1.0)
    except Exception:
        logger.exception("[EXIT UNFILLED REPRICE] retry execute_exit failed symbol=%s", symbol)


def _loop() -> None:
    try:
        import force_cancel_loop as fcl
    except Exception:
        logger.exception("[EXIT UNFILLED REPRICE] import force_cancel_loop failed")
        return
    get_orders = getattr(fcl, "get_orders", None)
    cancel_order = getattr(fcl, "cancel_order", None)
    if not callable(get_orders) or not callable(cancel_order):
        logger.warning("[EXIT UNFILLED REPRICE] get_orders/cancel_order missing")
        return

    interval = max(0.2, _env_float("EXIT_UNFILLED_CHECK_INTERVAL_SEC", 0.5))
    cancel_after = max(0.5, _env_float("EXIT_UNFILLED_CANCEL_SEC", 1.2))
    cancelable_states = set(getattr(fcl, "CANCELABLE_STATES", {1, 2, 3, 4}))
    logger.warning("[EXIT UNFILLED REPRICE] loop start interval=%.2fs cancel_after=%.2fs", interval, cancel_after)

    while True:
        active: set[str] = set()
        try:
            now_ts = time.time()
            for order in get_orders() or []:
                if not isinstance(order, dict):
                    continue
                order_id = str(order.get("OrderId") or order.get("ID") or "").strip()
                if not order_id:
                    continue
                active.add(order_id)
                if not _is_close_limit_order(order, cancelable_states):
                    continue
                symbol = _sym(order.get("Symbol"))
                qty, cum, leaves = _remaining(order)
                elapsed = _order_elapsed(order_id, order, now_ts)
                logger.warning("[EXIT UNFILLED REPRICE] watch order_id=%s symbol=%s elapsed=%.2fs remain=%.0f qty=%.0f cum=%.0f price=%s", order_id, symbol, elapsed, leaves, qty, cum, order.get("Price"))
                if elapsed < cancel_after:
                    continue
                if now_ts - _CANCEL_SENT.get(order_id, 0.0) < 1.0:
                    continue
                logger.warning("[EXIT UNFILLED REPRICE] cancel close order_id=%s symbol=%s elapsed=%.2fs", order_id, symbol, elapsed)
                cancel_order(order_id)
                _CANCEL_SENT[order_id] = now_ts
                _restore_position_open(symbol)
                if _env_bool("EXIT_UNFILLED_REPRICE_ENABLED", True):
                    _retry_exit(symbol, order)
                time.sleep(0.1)
            for oid in list(_FIRST_SEEN.keys()):
                if oid not in active:
                    _FIRST_SEEN.pop(oid, None)
                    _CANCEL_SENT.pop(oid, None)
        except Exception:
            logger.exception("[EXIT UNFILLED REPRICE] loop error")
        time.sleep(interval)


def install() -> bool:
    global _INSTALLED, _THREAD_STARTED
    if _INSTALLED:
        return True
    if not _env_bool("EXIT_UNFILLED_REPRICE_ENABLED", True):
        logger.warning("[EXIT UNFILLED REPRICE] disabled")
        return False
    if not _THREAD_STARTED:
        threading.Thread(target=_loop, daemon=True, name="exit_unfilled_reprice_loop").start()
        _THREAD_STARTED = True
    _INSTALLED = True
    logger.warning(
        "[EXIT UNFILLED REPRICE] installed cancel_sec=%.2f max_rounds=%s market_on_final=%s",
        _env_float("EXIT_UNFILLED_CANCEL_SEC", 1.2),
        _env_int("EXIT_UNFILLED_REPRICE_MAX_ROUNDS", 1),
        _env_bool("EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL", True),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT UNFILLED REPRICE] auto install failed")


__all__ = ["install"]
