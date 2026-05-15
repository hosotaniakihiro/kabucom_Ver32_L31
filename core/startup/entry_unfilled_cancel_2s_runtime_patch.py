# ============================================================
# File   : core/startup/entry_unfilled_cancel_2s_runtime_patch.py
# Version: V1.1-CANCEL-AND-NEXT
# ------------------------------------------------------------
# 未約定の新規指値注文を2秒で取消し、取消後に次候補のENTRYを起動する。
#
# - 監視間隔: 0.5秒
# - 未約定取消: 2秒
# - 取消銘柄は30秒だけ再エントリー抑止
# - 取消後に entry_controller.run_entry_pipeline() を呼ぶ
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
_FIRST_SEEN: Dict[str, float] = {}
_CANCEL_SENT: Dict[str, float] = {}
_NEXT_ENTRY_RUNNING = False
_NEXT_ENTRY_LOCK = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v in (None, ""):
        return bool(default)
    return str(v).lower() in {"1", "true", "yes", "y", "on"}


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v)) if v not in (None, "") else int(default)
    except Exception:
        return int(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _order_time_elapsed(order_id: str, order: Dict[str, Any], now_ts: float) -> float:
    # kabu APIの時刻項目は環境により揺れるため、読めない場合は初回検知から計測する。
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
        logger.warning("[ENTRY CANCEL 2S] first_seen order_id=%s symbol=%s", order_id, order.get("Symbol"))
    return max(0.0, now_ts - _FIRST_SEEN[order_id])


def _remaining(order: Dict[str, Any]) -> tuple[float, float, float]:
    qty = _f(order.get("OrderQty", order.get("Qty", 0)), 0.0)
    cum = _f(order.get("CumQty", order.get("ContractedQty", 0)), 0.0)
    leaves = _f(order.get("LeavesQty", 0), 0.0)
    if leaves <= 0 and qty > 0:
        leaves = max(qty - cum, 0.0)
    if qty <= 0 and leaves > 0:
        qty = leaves + cum
    return qty, cum, leaves


def _is_target_order(order: Dict[str, Any], cancelable_states: set[int]) -> bool:
    state = _i(order.get("State"), 0)
    price = _f(order.get("Price"), 0.0)
    front = _i(order.get("FrontOrderType"), 0)
    cash_margin = _i(order.get("CashMargin"), 0)
    qty, cum, leaves = _remaining(order)
    is_limit = front == 20 or (front != 10 and price > 0)
    is_open = leaves > 0 or (qty > 0 and cum < qty)
    is_new_entry = cash_margin != 3  # 3=返済は対象外。不明/2=新規は対象。
    return bool(is_limit and is_open and is_new_entry and state in cancelable_states)


def _mark_cancelled_symbol(symbol: str) -> None:
    try:
        from global_state import global_data
        cooldown = _env_float("ENTRY_CANCEL_SYMBOL_COOLDOWN_SEC", 30.0)
        if not hasattr(global_data, "trade_restricted") or not isinstance(global_data.trade_restricted, dict):
            global_data.trade_restricted = {}
        until = dt.datetime.now() + dt.timedelta(seconds=cooldown)
        global_data.trade_restricted[_sym(symbol)] = until
        logger.warning("[ENTRY CANCEL 2S] symbol cooldown symbol=%s until=%s", symbol, until)
    except Exception:
        logger.exception("[ENTRY CANCEL 2S] symbol cooldown failed symbol=%s", symbol)


def _clear_inflight(symbol: str, order_id: str) -> None:
    try:
        from global_state import global_data
        for attr in ("entry_inflight", "entry_inflight_orders", "inflight_entries"):
            m = getattr(global_data, attr, None)
            if isinstance(m, dict):
                m.pop(_sym(symbol), None)
                m.pop(str(order_id), None)
    except Exception:
        logger.debug("[ENTRY CANCEL 2S] clear inflight failed", exc_info=True)


def _trigger_next_entry() -> None:
    global _NEXT_ENTRY_RUNNING
    if not _env_bool("ENTRY_CANCEL_TRIGGER_NEXT", True):
        return
    with _NEXT_ENTRY_LOCK:
        if _NEXT_ENTRY_RUNNING:
            return
        _NEXT_ENTRY_RUNNING = True
    def _run():
        global _NEXT_ENTRY_RUNNING
        try:
            time.sleep(_env_float("ENTRY_CANCEL_NEXT_DELAY_SEC", 0.2))
            from trading.handlers.entry_controller import run_entry_pipeline
            logger.warning("[ENTRY CANCEL 2S] trigger next entry pipeline")
            run_entry_pipeline()
        except Exception:
            logger.exception("[ENTRY CANCEL 2S] trigger next entry failed")
        finally:
            with _NEXT_ENTRY_LOCK:
                _NEXT_ENTRY_RUNNING = False
    threading.Thread(target=_run, daemon=True, name="entry_cancel_trigger_next").start()


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import force_cancel_loop as fcl
    except Exception:
        logger.exception("[ENTRY CANCEL 2S] import force_cancel_loop failed")
        return False
    get_orders = getattr(fcl, "get_orders", None)
    cancel_order = getattr(fcl, "cancel_order", None)
    if not callable(get_orders) or not callable(cancel_order):
        logger.warning("[ENTRY CANCEL 2S] get_orders/cancel_order missing")
        return False

    def start_force_cancel_loop_2s(interval_sec=None):
        interval = max(0.2, _env_float("ENTRY_CANCEL_CHECK_INTERVAL_SEC", 0.5))
        cancel_after = max(0.5, _env_float("ENTRY_UNFILLED_CANCEL_SEC", 2.0))
        cancelable_states = set(getattr(fcl, "CANCELABLE_STATES", {1, 2, 3, 4}))
        logger.warning("[ENTRY CANCEL 2S] loop start interval=%.2fs cancel_after=%.2fs", interval, cancel_after)
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
                    if not _is_target_order(order, cancelable_states):
                        continue
                    symbol = _sym(order.get("Symbol"))
                    qty, cum, leaves = _remaining(order)
                    elapsed = _order_time_elapsed(order_id, order, now_ts)
                    logger.warning("[ENTRY CANCEL 2S] watch order_id=%s symbol=%s elapsed=%.2fs remain=%.0f qty=%.0f cum=%.0f price=%s", order_id, symbol, elapsed, leaves, qty, cum, order.get("Price"))
                    if elapsed < cancel_after:
                        continue
                    if now_ts - _CANCEL_SENT.get(order_id, 0.0) < 1.0:
                        continue
                    logger.warning("[ENTRY CANCEL 2S] cancel and next order_id=%s symbol=%s elapsed=%.2fs", order_id, symbol, elapsed)
                    cancel_order(order_id)
                    _CANCEL_SENT[order_id] = now_ts
                    _clear_inflight(symbol, order_id)
                    _mark_cancelled_symbol(symbol)
                    _trigger_next_entry()
                    time.sleep(0.1)
                for oid in list(_FIRST_SEEN.keys()):
                    if oid not in active:
                        _FIRST_SEEN.pop(oid, None)
                        _CANCEL_SENT.pop(oid, None)
            except Exception:
                logger.exception("[ENTRY CANCEL 2S] loop error")
            time.sleep(interval)

    start_force_cancel_loop_2s._entry_cancel_2s_wrapped = True  # type: ignore[attr-defined]
    fcl.start_force_cancel_loop = start_force_cancel_loop_2s
    _INSTALLED = True
    logger.warning("[ENTRY CANCEL 2S] installed cancel_after=2s trigger_next=True")
    return True

try:
    install()
except Exception:
    logger.exception("[ENTRY CANCEL 2S] auto install failed")

__all__ = ["install"]
