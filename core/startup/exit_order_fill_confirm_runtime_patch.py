# ============================================================
# File   : core/startup/exit_order_fill_confirm_runtime_patch.py
# Version: V1-CONFIRM-CLOSING-EXIT-ORDER-FILL
# ------------------------------------------------------------
# 指値EXITを注文受付時点でCLOSINGにした後、kabusapi/orders を監視して
# 全約定ならpositions.dbを正式CLOSEDに確定する。
#
# 目的:
#   - 未約定のままCLOSEDにしない
#   - 約定済みなのにCLOSINGで残さない
#   - 部分約定はCLOSING維持
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


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _order_id(v: Any) -> str:
    return str(v or "").strip()


def _remaining(order: Dict[str, Any]) -> tuple[float, float, float]:
    qty = _f(order.get("OrderQty", order.get("Qty", 0)), 0.0)
    cum = _f(order.get("CumQty", order.get("ContractedQty", 0)), 0.0)
    leaves = _f(order.get("LeavesQty", 0), 0.0)
    if leaves <= 0 and qty > 0:
        leaves = max(qty - cum, 0.0)
    if qty <= 0 and leaves > 0:
        qty = leaves + cum
    return qty, cum, leaves


def _is_filled(order: Dict[str, Any]) -> bool:
    state = str(order.get("State") or "").strip()
    qty, cum, leaves = _remaining(order)
    # kabuS APIでは状態値の詳細が環境差で揺れるため、数量を主判定にする。
    if qty > 0 and cum >= qty and leaves <= 0:
        return True
    if leaves <= 0 and cum > 0 and state not in {"1", "2", "3", "4"}:
        return True
    return False


def _find_order(orders: list[dict], wanted_order_id: str, symbol: str) -> dict | None:
    wanted_order_id = _order_id(wanted_order_id)
    symbol = _sym(symbol)
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        oid = _order_id(o.get("OrderId") or o.get("ID"))
        if wanted_order_id and oid == wanted_order_id:
            return o
    # order_idが空/欠落した古いDB行向けの補助。返済注文だけを拾う。
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        if _sym(o.get("Symbol")) != symbol:
            continue
        try:
            if int(float(o.get("CashMargin") or 0)) == 3:
                return o
        except Exception:
            pass
    return None


def _close_db_and_memory(symbol: str, order_id: str, order: dict) -> bool:
    symbol = _sym(symbol)
    session = None
    try:
        from database import Session_position
        from database.models import Position
        session = Session_position()
        row = (
            session.query(Position)
            .filter(Position.symbol == symbol)
            .filter(Position.status == "CLOSING")
            .first()
        )
        if not row:
            session.rollback()
            return False
        now = dt.datetime.now()
        avg_price = _f(getattr(row, "avg_price", 0) or getattr(row, "entry_price", 0) or getattr(row, "price", 0), 0.0)
        qty = _f(getattr(row, "qty", 0), 0.0)
        exit_price = _f(order.get("Price"), 0.0) or _f(getattr(row, "exit_price", 0), 0.0)
        side = str(getattr(row, "side", "") or "").upper()
        pnl = 0.0
        if qty > 0 and avg_price > 0 and exit_price > 0:
            pnl = (exit_price - avg_price) * qty if side == "BUY" else (avg_price - exit_price) * qty
        row.status = "CLOSED"
        for attr, value in [
            ("exit_price", exit_price),
            ("exit_time", now),
            ("closed_time", now),
            ("close_time", now),
            ("updated_at", now),
            ("pnl", pnl),
            ("exit_order_id", order_id),
            ("order_id", order_id),
        ]:
            try:
                if hasattr(row, attr):
                    setattr(row, attr, value)
            except Exception:
                pass
        session.commit()
        try:
            from global_state import global_data
            positions = getattr(global_data, "open_positions", None)
            if isinstance(positions, dict):
                positions.pop(symbol, None)
        except Exception:
            pass
        logger.warning("[EXIT FILL CONFIRM] CLOSED confirmed symbol=%s order_id=%s exit_price=%.4f pnl=%.4f", symbol, order_id, exit_price, pnl)
        return True
    except Exception:
        try:
            if session is not None:
                session.rollback()
        except Exception:
            pass
        logger.exception("[EXIT FILL CONFIRM] close DB failed symbol=%s order_id=%s", symbol, order_id)
        return False
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass


def _closing_rows() -> list[dict]:
    out: list[dict] = []
    session = None
    try:
        from database import Session_position
        from database.models import Position
        session = Session_position()
        rows = session.query(Position).filter(Position.status == "CLOSING").all()
        for r in rows or []:
            sym = _sym(getattr(r, "symbol", ""))
            oid = _order_id(getattr(r, "exit_order_id", "") or getattr(r, "order_id", ""))
            if sym:
                out.append({"symbol": sym, "order_id": oid})
    except Exception:
        logger.debug("[EXIT FILL CONFIRM] load CLOSING rows failed", exc_info=True)
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass
    return out


def _loop() -> None:
    try:
        import force_cancel_loop as fcl
    except Exception:
        logger.exception("[EXIT FILL CONFIRM] import force_cancel_loop failed")
        return
    get_orders = getattr(fcl, "get_orders", None)
    if not callable(get_orders):
        logger.warning("[EXIT FILL CONFIRM] get_orders missing")
        return
    interval = max(0.5, _env_float("EXIT_FILL_CONFIRM_INTERVAL_SEC", 1.0))
    logger.warning("[EXIT FILL CONFIRM] loop start interval=%.2fs", interval)
    while True:
        try:
            rows = _closing_rows()
            if not rows:
                time.sleep(interval)
                continue
            orders = get_orders() or []
            for row in rows:
                sym = row.get("symbol") or ""
                oid = row.get("order_id") or ""
                order = _find_order(orders, oid, sym)
                if order and _is_filled(order):
                    _close_db_and_memory(sym, oid or _order_id(order.get("OrderId") or order.get("ID")), order)
                elif order:
                    qty, cum, leaves = _remaining(order)
                    logger.info("[EXIT FILL CONFIRM] still closing symbol=%s order_id=%s cum=%.0f leaves=%.0f qty=%.0f", sym, oid, cum, leaves, qty)
                else:
                    logger.debug("[EXIT FILL CONFIRM] order not found yet symbol=%s order_id=%s", sym, oid)
        except Exception:
            logger.exception("[EXIT FILL CONFIRM] loop error")
        time.sleep(interval)


def install() -> bool:
    global _INSTALLED, _THREAD_STARTED
    if _INSTALLED:
        return True
    if not _env_bool("EXIT_FILL_CONFIRM_ENABLED", True):
        logger.warning("[EXIT FILL CONFIRM] disabled")
        return False
    if not _THREAD_STARTED:
        threading.Thread(target=_loop, daemon=True, name="exit_fill_confirm_loop").start()
        _THREAD_STARTED = True
    _INSTALLED = True
    logger.warning("[EXIT FILL CONFIRM] installed interval=%.2f", _env_float("EXIT_FILL_CONFIRM_INTERVAL_SEC", 1.0))
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT FILL CONFIRM] auto install failed")


__all__ = ["install"]
