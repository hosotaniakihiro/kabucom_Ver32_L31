# ============================================================
# File   : trading/handlers/pending_order_monitor.py
# Version: Ver1.2-PASSIVE-ENTRY-CANCEL-RETRY-BOARD-ADVERSE
# ------------------------------------------------------------
# ✔ ENTRY発注成功後のOrderIdを監視
# ✔ 未約定/未取消注文へ cancelorder を送る
# ✔ 厚板1ティック手前指値向けに、既定4秒で取消
# ✔ 取消後、同一銘柄は最大1回だけ即再試行を許可
# ✔ 2回目以降は同一銘柄だけクールダウン
# ✔ 反対方向へ板が動いたら cancel_after 前でも早期取消
# ✔ 別銘柄のエントリーは止めない
# ✔ pending_entries とは分離し global_data.pending_orders を使う
# ✔ register_pending_entry_order() 呼び出し時に監視スレッドを自動起動
# ✔ symbol単位の entry_inflight も解除
#
# ENV:
#   ENTRY_PASSIVE_CANCEL_AFTER_SEC=4
#   ENTRY_PASSIVE_RETRY_MAX=1
#   ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS=60
#   ENTRY_CANCEL_BOARD_ADVERSE_ENABLED=1
#   ENTRY_CANCEL_BOARD_ADVERSE_TICKS=2
#   ENTRY_CANCEL_BOARD_EXCHANGE=1
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import threading
import time
from typing import Any, Dict

from global_state import global_data
from kabu_api.cancel_order import cancel_order_common

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


# 厚板1ティック手前の指値は、長く置くと置いていかれる/逆行するため短めに監視する。
ENTRY_UNFILLED_CANCEL_SECONDS = _env_float("ENTRY_PASSIVE_CANCEL_AFTER_SEC", _env_float("ENTRY_UNFILLED_CANCEL_SECONDS", 4.0))
MONITOR_INTERVAL_SECONDS = _env_float("ENTRY_CANCEL_MONITOR_INTERVAL_SEC", 0.5)
CANCEL_REQUEST_CLEANUP_SECONDS = _env_float("ENTRY_CANCEL_REQUEST_CLEANUP_SEC", 5.0)
ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS = _env_float("ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS", 60.0)
ENTRY_PASSIVE_RETRY_MAX = _env_int("ENTRY_PASSIVE_RETRY_MAX", 1)
ENTRY_CANCEL_BOARD_ADVERSE_ENABLED = _env_bool("ENTRY_CANCEL_BOARD_ADVERSE_ENABLED", True)
ENTRY_CANCEL_BOARD_ADVERSE_TICKS = _env_int("ENTRY_CANCEL_BOARD_ADVERSE_TICKS", 2)
ENTRY_CANCEL_BOARD_EXCHANGE = _env_int("ENTRY_CANCEL_BOARD_EXCHANGE", _env_int("ENTRY_BOARD_EXCHANGE", 1))

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
    try:
        cd = getattr(global_data, "entry_cancel_cooldown", None)
        if not isinstance(cd, dict):
            setattr(global_data, "entry_cancel_cooldown", {})
        return getattr(global_data, "entry_cancel_cooldown")
    except Exception:
        setattr(global_data, "entry_cancel_cooldown", {})
        return getattr(global_data, "entry_cancel_cooldown")


def _ensure_cancel_count() -> Dict[str, int]:
    try:
        mp = getattr(global_data, "entry_cancel_retry_count", None)
        if not isinstance(mp, dict):
            setattr(global_data, "entry_cancel_retry_count", {})
        return getattr(global_data, "entry_cancel_retry_count")
    except Exception:
        setattr(global_data, "entry_cancel_retry_count", {})
        return getattr(global_data, "entry_cancel_retry_count")


def _mark_symbol_cancel_cooldown(symbol: str, reason: str = "entry_unfilled_cancel"):
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


def _mark_cancel_and_decide_retry(symbol: str, reason: str) -> bool:
    """Trueなら同一銘柄の即再試行を許可。Falseならクールダウン。"""
    sym = str(symbol or "").strip()
    if not sym:
        return False
    mp = _ensure_cancel_count()
    cnt = int(mp.get(sym) or 0) + 1
    mp[sym] = cnt
    allow_retry = cnt <= max(0, int(ENTRY_PASSIVE_RETRY_MAX))
    logger.warning(
        "[ENTRY CANCEL RETRY JUDGE] symbol=%s cancel_count=%s retry_max=%s allow_retry=%s reason=%s",
        sym,
        cnt,
        ENTRY_PASSIVE_RETRY_MAX,
        allow_retry,
        reason,
    )
    return allow_retry


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
            logger.warning("[ENTRY CANCEL WATCH] entry_inflight released by method symbol=%s reason=%s", sym, reason)
            return
    except Exception:
        logger.exception("[ENTRY CANCEL WATCH] release_entry_inflight method failed symbol=%s", sym)

    try:
        inflight = getattr(global_data, "entry_inflight", None)
        if hasattr(inflight, "discard"):
            inflight.discard(sym)
            logger.warning("[ENTRY CANCEL WATCH] entry_inflight discarded symbol=%s reason=%s", sym, reason)
        elif hasattr(inflight, "remove"):
            try:
                inflight.remove(sym)
            except KeyError:
                pass
            logger.warning("[ENTRY CANCEL WATCH] entry_inflight removed symbol=%s reason=%s", sym, reason)
    except Exception:
        logger.exception("[ENTRY CANCEL WATCH] inflight release failed symbol=%s", sym)


def _tick_size(price: float) -> float:
    p = float(price or 0.0)
    if p <= 0:
        return 1.0
    if p <= 1000:
        return 0.1
    if p <= 3000:
        return 0.5
    if p <= 5000:
        return 1.0
    if p <= 30000:
        return 5.0
    if p <= 50000:
        return 10.0
    if p <= 300000:
        return 50.0
    if p <= 500000:
        return 100.0
    if p <= 3000000:
        return 500.0
    return 1000.0


def _is_board_adverse(symbol: str, side: str, order_price: Any) -> tuple[bool, str, dict[str, Any]]:
    if not ENTRY_CANCEL_BOARD_ADVERSE_ENABLED:
        return False, "disabled", {}
    try:
        px = float(order_price or 0.0)
        if px <= 0:
            return False, "no_order_price", {}
        from trading.board.board_client import fetch_board_snapshot
        snap = fetch_board_snapshot(symbol, exchange=ENTRY_CANCEL_BOARD_EXCHANGE, timeout=0.8, levels=3)
        if not isinstance(snap, dict):
            return False, "board_missing", {}
        bid = float(snap.get("best_bid") or 0.0)
        ask = float(snap.get("best_ask") or 0.0)
        tick = _tick_size(px)
        ticks = max(0, int(ENTRY_CANCEL_BOARD_ADVERSE_TICKS))
        side_u = str(side or "").upper()
        detail = {"bid": bid, "ask": ask, "order_price": px, "tick": tick, "ticks": ticks}
        # BUY: 売り気配が指値より大きく上へ離れたら、置いていかれた/追いかけ危険として取消。
        if side_u == "BUY" and ask > 0 and ask >= px + tick * ticks:
            return True, "buy_ask_moved_away", detail
        # SELL: 買い気配が指値より大きく下へ離れたら、置いていかれた/追いかけ危険として取消。
        if side_u == "SELL" and bid > 0 and bid <= px - tick * ticks:
            return True, "sell_bid_moved_away", detail
        return False, "ok", detail
    except Exception:
        logger.debug("[ENTRY CANCEL WATCH] board adverse check failed symbol=%s", symbol, exc_info=True)
        return False, "exception", {}


def register_pending_entry_order(*, order_id: str, symbol: str, side: str, qty: int, price: float | None = None, source: str = "ENTRY", cancel_after_sec: float | None = None) -> bool:
    """発注成功直後に呼ぶ。OrderId を登録し、一定時間後/板逆行時に cancelorder を送る。"""
    start_pending_order_monitor()

    oid = str(order_id or "").strip()
    sym = str(symbol or "").strip()
    if not oid or not sym:
        logger.error("[ENTRY CANCEL WATCH] register skipped invalid order_id=%s symbol=%s", oid, sym)
        return False

    ca = ENTRY_UNFILLED_CANCEL_SECONDS if cancel_after_sec is None else float(cancel_after_sec)
    po = _ensure_pending_orders()
    po[oid] = {
        "order_id": oid,
        "symbol": sym,
        "side": str(side or "").upper(),
        "qty": int(qty or 0),
        "price": float(price) if price is not None else None,
        "source": str(source or "ENTRY"),
        "created_at": _now(),
        "cancel_after_sec": float(ca),
        "cancel_requested": False,
        "cancel_requested_at": None,
    }

    logger.warning(
        "[ENTRY CANCEL WATCH] registered order_id=%s symbol=%s side=%s qty=%s price=%s source=%s cancel_after=%.1fs adverse_board=%s retry_max=%s",
        oid,
        sym,
        str(side or "").upper(),
        int(qty or 0),
        price,
        source,
        float(ca),
        ENTRY_CANCEL_BOARD_ADVERSE_ENABLED,
        ENTRY_PASSIVE_RETRY_MAX,
    )
    return True


def _cancel_pending_order(oid: str, info: dict, reason: str, detail: dict[str, Any] | None = None):
    symbol = str(info.get("symbol") or "").strip()
    logger.warning(
        "[ENTRY CANCEL WATCH] cancel entry order_id=%s symbol=%s side=%s reason=%s qty=%s price=%s detail=%s",
        oid,
        symbol,
        info.get("side"),
        reason,
        info.get("qty"),
        info.get("price"),
        detail or {},
    )
    ok = cancel_order_common(oid, symbol=symbol, reason=reason)
    info["cancel_requested"] = True
    info["cancel_requested_at"] = _now()
    info["cancel_result"] = bool(ok)

    po = _ensure_pending_orders()
    po.pop(oid, None)
    _release_entry_inflight(symbol, reason=reason)

    allow_retry = _mark_cancel_and_decide_retry(symbol, reason)
    if not allow_retry:
        _mark_symbol_cancel_cooldown(symbol, reason=reason)

    logger.warning(
        "[ENTRY CANCEL WATCH] cancel requested order_id=%s symbol=%s ok=%s allow_retry=%s cooldown_if_no_retry=%.1fs another_symbols_allowed=True",
        oid,
        symbol,
        ok,
        allow_retry,
        ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS,
    )


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

        if info.get("cancel_requested"):
            requested_at = float(info.get("cancel_requested_at") or now)
            if now - requested_at >= CANCEL_REQUEST_CLEANUP_SECONDS:
                logger.warning("[ENTRY CANCEL WATCH] cleanup after cancel request order_id=%s symbol=%s", oid, symbol)
                po.pop(oid, None)
                _release_entry_inflight(symbol, reason="entry_cancel_cleanup")
            continue

        adverse, adverse_reason, adverse_detail = _is_board_adverse(symbol, str(info.get("side") or ""), info.get("price"))
        if adverse:
            _cancel_pending_order(oid, info, f"entry_board_adverse:{adverse_reason}", adverse_detail)
            continue

        if elapsed >= cancel_after:
            _cancel_pending_order(oid, info, "entry_unfilled_cancel_timeout", {"elapsed": elapsed, "cancel_after": cancel_after})
            continue


def _monitor_loop():
    logger.warning(
        "[ENTRY CANCEL WATCH] monitor started interval=%.1fs cancel_after=%.1fs retry_max=%s cooldown=%.1fs adverse_board=%s adverse_ticks=%s",
        MONITOR_INTERVAL_SECONDS,
        ENTRY_UNFILLED_CANCEL_SECONDS,
        ENTRY_PASSIVE_RETRY_MAX,
        ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS,
        ENTRY_CANCEL_BOARD_ADVERSE_ENABLED,
        ENTRY_CANCEL_BOARD_ADVERSE_TICKS,
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
        thread = threading.Thread(target=_monitor_loop, name="entry-pending-order-cancel-monitor", daemon=True)
        thread.start()
        _started = True
        return True


__all__ = ["ENTRY_UNFILLED_CANCEL_SECONDS", "ENTRY_CANCEL_SYMBOL_COOLDOWN_SECONDS", "register_pending_entry_order", "start_pending_order_monitor"]
