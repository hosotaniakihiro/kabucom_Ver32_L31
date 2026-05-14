# ============================================================
# File   : trading/handlers/pending_order_monitor.py
# Version: Ver1.1-PRODUCTION-ENTRY-UNFILLED-CANCEL-2SEC-SYMBOL-COOLDOWN
# ------------------------------------------------------------
# ✔ ENTRY発注成功後のOrderIdを監視
# ✔ 2秒経過した未約定/未取消注文へ cancelorder を送る
# ✔ キャンセルした銘柄だけ60秒再エントリー禁止
# ✔ 別銘柄のエントリーは止めない
# ✔ pending_entries とは分離し global_data.pending_orders を使う
# ✔ register_pending_entry_order() 呼び出し時に監視スレッドを自動起動
# ✔ symbol単位の entry_inflight も解除
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Dict

from global_state import global_data
from kabu_api.cancel_order import cancel_order_common

logger = logging.getLogger(__name__)

# 短期順張りでは、刺さらない注文を長く待つと高値掴み/安値売りになりやすい。
ENTRY_UNFILLED_CANCEL_SECONDS = 2.0
MONITOR_INTERVAL_SECONDS = 0.5
CANCEL_REQUEST_CLEANUP_SECONDS = 5.0

# 未約定キャンセルした銘柄だけ、一定時間追いかけない。
# entry_controller は global_data.trade_restricted を既に参照しているため、ここへ入れる。
ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS = 60.0

_started = False
_started_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _ensure_pending_orders() -> Dict[str, dict]:
    try:
        po = getattr(global_data, "pending_orders", None)
        if not isinstance(po, dict):
            setattr(global_data, "pending_orders", {})
        return getattr(global_data, "pending_orders")
    except Exception:
        setattr(global_data, "pending_orders", {})
        return getattr(global_data, "pending_orders")


def _ensure_cancel_cooldown() -> Dict[str, float]:
    """
    診断用に epoch 秒でも保持する。
    実際のエントリーskipは entry_controller 既存の trade_restricted を使う。
    """
    try:
        cd = getattr(global_data, "entry_cancel_cooldown", None)
        if not isinstance(cd, dict):
            setattr(global_data, "entry_cancel_cooldown", {})
        return getattr(global_data, "entry_cancel_cooldown")
    except Exception:
        setattr(global_data, "entry_cancel_cooldown", {})
        return getattr(global_data, "entry_cancel_cooldown")


def _mark_symbol_cancel_cooldown(symbol: str, reason: str = "entry_unfilled_cancel"):
    """
    未約定キャンセルした銘柄だけ一時停止する。
    別銘柄のエントリーは止めない。
    """
    sym = str(symbol or "").strip()
    if not sym:
        return

    try:
        until_epoch = _now() + ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS
        _ensure_cancel_cooldown()[sym] = until_epoch

        trade_restricted = getattr(global_data, "trade_restricted", None)
        if not isinstance(trade_restricted, dict):
            setattr(global_data, "trade_restricted", {})
            trade_restricted = getattr(global_data, "trade_restricted")

        until_dt = dt.datetime.now() + dt.timedelta(seconds=ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS)
        trade_restricted[sym] = until_dt

        logger.warning(
            "[ENTRY CANCEL COOLDOWN] symbol=%s cooldown=%.1fs until=%s reason=%s another_symbols_allowed=True",
            sym,
            ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS,
            until_dt,
            reason,
        )
    except Exception:
        logger.exception("[ENTRY CANCEL COOLDOWN] failed symbol=%s", sym)


def _release_entry_inflight(symbol: str, reason: str = ""):
    sym = str(symbol or "").strip()
    if not sym:
        return

    try:
        fn = getattr(global_data, "release_entry_inflight", None)
        if callable(fn):
            try:
                fn(sym, reason=reason)
            except TypeError:
                fn(sym)
            logger.warning(
                "[ENTRY CANCEL WATCH] entry_inflight released by method symbol=%s reason=%s",
                sym,
                reason,
            )
            return
    except Exception:
        logger.exception("[ENTRY CANCEL WATCH] release_entry_inflight method failed symbol=%s", sym)

    try:
        inflight = getattr(global_data, "entry_inflight", None)
        if hasattr(inflight, "discard"):
            inflight.discard(sym)
            logger.warning(
                "[ENTRY CANCEL WATCH] entry_inflight discarded symbol=%s reason=%s",
                sym,
                reason,
            )
        elif hasattr(inflight, "remove"):
            try:
                inflight.remove(sym)
            except KeyError:
                pass
            logger.warning(
                "[ENTRY CANCEL WATCH] entry_inflight removed symbol=%s reason=%s",
                sym,
                reason,
            )
    except Exception:
        logger.exception("[ENTRY CANCEL WATCH] inflight release failed symbol=%s", sym)


def register_pending_entry_order(
    *,
    order_id: str,
    symbol: str,
    side: str,
    qty: int,
    price: float | None = None,
    source: str = "ENTRY",
    cancel_after_sec: float = ENTRY_UNFILLED_CANCEL_SECONDS,
) -> bool:
    """
    発注成功直後に呼ぶ。
    OrderId を登録し、cancel_after_sec 経過後に cancelorder を送る。
    """
    start_pending_order_monitor()

    oid = str(order_id or "").strip()
    sym = str(symbol or "").strip()

    if not oid or not sym:
        logger.error(
            "[ENTRY CANCEL WATCH] register skipped invalid order_id=%s symbol=%s",
            oid,
            sym,
        )
        return False

    po = _ensure_pending_orders()
    po[oid] = {
        "order_id": oid,
        "symbol": sym,
        "side": str(side or "").upper(),
        "qty": int(qty or 0),
        "price": float(price) if price is not None else None,
        "source": str(source or "ENTRY"),
        "created_at": _now(),
        "cancel_after_sec": float(cancel_after_sec),
        "cancel_requested": False,
        "cancel_requested_at": None,
    }

    logger.warning(
        "[ENTRY CANCEL WATCH] registered order_id=%s symbol=%s side=%s qty=%s price=%s source=%s cancel_after=%.1fs",
        oid,
        sym,
        str(side or "").upper(),
        int(qty or 0),
        price,
        source,
        float(cancel_after_sec),
    )
    return True


def _monitor_once():
    po = _ensure_pending_orders()
    if not po:
        return

    now = _now()

    for oid, info in list(po.items()):
        if not isinstance(info, dict):
            logger.warning("[ENTRY CANCEL WATCH] drop invalid pending order_id=%s info=%s", oid, info)
            po.pop(oid, None)
            continue

        symbol = str(info.get("symbol") or "").strip()
        created_at = float(info.get("created_at") or now)
        elapsed = now - created_at
        cancel_after = float(info.get("cancel_after_sec") or ENTRY_UNFILLED_CANCEL_SECONDS)

        if elapsed < cancel_after:
            continue

        if info.get("cancel_requested"):
            requested_at = float(info.get("cancel_requested_at") or now)
            if now - requested_at >= CANCEL_REQUEST_CLEANUP_SECONDS:
                logger.warning(
                    "[ENTRY CANCEL WATCH] cleanup after cancel request order_id=%s symbol=%s",
                    oid,
                    symbol,
                )
                po.pop(oid, None)
                _release_entry_inflight(symbol, reason="entry_cancel_cleanup")
            continue

        logger.warning(
            "[ENTRY CANCEL WATCH] cancel unfilled entry order_id=%s symbol=%s side=%s elapsed=%.1fs qty=%s price=%s",
            oid,
            symbol,
            info.get("side"),
            elapsed,
            info.get("qty"),
            info.get("price"),
        )

        ok = cancel_order_common(oid, symbol=symbol, reason="entry_unfilled_cancel_2sec")

        info["cancel_requested"] = True
        info["cancel_requested_at"] = now
        info["cancel_result"] = bool(ok)

        # 取消要求後は詰まり防止のため監視対象から外す。
        # すでに約定済みならcancel APIは失敗することがあるが、inflightは解除する。
        po.pop(oid, None)
        _release_entry_inflight(symbol, reason="entry_unfilled_cancel_2sec")
        _mark_symbol_cancel_cooldown(symbol, reason="entry_unfilled_cancel_2sec")

        logger.warning(
            "[ENTRY CANCEL WATCH] cancel requested order_id=%s symbol=%s ok=%s next_same_symbol_blocked=%.1fs another_symbols_allowed=True",
            oid,
            symbol,
            ok,
            ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS,
        )


def _monitor_loop():
    logger.warning(
        "[ENTRY CANCEL WATCH] monitor started interval=%.1fs cancel_after=%.1fs symbol_cooldown=%.1fs",
        MONITOR_INTERVAL_SECONDS,
        ENTRY_UNFILLED_CANCEL_SECONDS,
        ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS,
    )

    while True:
        try:
            _monitor_once()
        except Exception:
            logger.exception("[ENTRY CANCEL WATCH] monitor loop error")
        time.sleep(MONITOR_INTERVAL_SECONDS)


def start_pending_order_monitor() -> bool:
    global _started

    with _started_lock:
        if _started:
            return False

        thread = threading.Thread(
            target=_monitor_loop,
            name="entry-pending-order-cancel-monitor",
            daemon=True,
        )
        thread.start()
        _started = True
        return True


__all__ = [
    "ENTRY_UNFILLED_CANCEL_SECONDS",
    "ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS",
    "register_pending_entry_order",
    "start_pending_order_monitor",
]
