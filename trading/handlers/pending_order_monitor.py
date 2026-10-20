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

# 2026-10-20: 買いの「少し下で待ち、離れたら今の買気配で出し直す」(ユーザー判断)。
#   entry_order_builder が発注直前に note_entry_order_intent() で発注時の買気配と待ち時間を置き、
#   register_pending_entry_order() が拾う。発注経路 (entry_handler) を通さずに渡すための受け渡し口。
#   - 「離れた」は発注時の買気配から ENTRY_CANCEL_BOARD_ADVERSE_TICKS 刻み上がったか (指値からではない)。
#     指値は買気配の 0.2% 下なので、指値から測ると発注した瞬間に「離れた」になる。
#   - 離れて取り消したときだけ出し直す (回数の上限は entry_retry_rotation の BUY 用上限)。
#     待ち時間切れは出し直さない (値が動かず下にも来なかった = 見送り)。
_ORDER_INTENT: Dict[str, dict] = {}
_ORDER_INTENT_TTL_SEC = 30.0


def note_entry_order_intent(symbol: str, side: str, *, mode: str, chase_round: int, ref_bid: float, cancel_after_sec: float) -> None:
    sym = str(symbol or "").strip()
    if not sym:
        return
    _ORDER_INTENT[sym] = {
        "side": str(side or "").upper(), "mode": str(mode), "chase_round": int(chase_round),
        "ref_bid": float(ref_bid or 0.0), "cancel_after_sec": float(cancel_after_sec), "ts": time.time(),
    }


def _take_order_intent(symbol: str, side: str) -> dict | None:
    it = _ORDER_INTENT.pop(str(symbol or "").strip(), None)
    if not it or time.time() - float(it.get("ts") or 0) > _ORDER_INTENT_TTL_SEC:
        return None
    if it.get("side") != str(side or "").upper():
        return None
    return it


def is_watched_order(order_id: str) -> bool:
    """この監視が待ち時間ごと受け持っている注文か (unfilled_order_cancel_watch の 10 秒取消を止めるため)。"""
    info = _ensure_pending_orders().get(str(order_id or "").strip())
    return isinstance(info, dict) and bool(info.get("mode")) and not info.get("cancel_requested")


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
        # 2026-10-07: 表の呼値は TOPIX100 用 (3,000 円以下 0.5 円) なので、ふつうの銘柄の 1 円スプレッドを
        #   「2 刻み離れた」と数え、買気配で置いた指値を発注 0.03 秒後に取り消していた (10/7 は 13 件、3103/2726/593A ...)。
        #   板に並んだ値段の間隔 (実際の刻み) を使い、取れない時だけ表に頼る。
        diffs = []
        for lad in (snap.get("sell") or [], snap.get("buy") or []):
            ps = sorted({float(x.get("price") or 0) for x in lad if isinstance(x, dict) and float(x.get("price") or 0) > 0})
            diffs += [round(b - a, 6) for a, b in zip(ps, ps[1:]) if b - a > 0]
        if ask > bid > 0:
            diffs.append(round(ask - bid, 6))
        tick = max(min(diffs), _tick_size(px)) if diffs else _tick_size(px)
        ticks = max(0, int(ENTRY_CANCEL_BOARD_ADVERSE_TICKS))
        side_u = str(side or "").upper()
        detail = {"bid": bid, "ask": ask, "order_price": px, "tick": tick, "ticks": ticks}
        # 2026-10-07: 「置いていかれた」は自分と同じ側の気配で見る。反対側の気配で見ると、買気配に置いた指値を
        #   スプレッドが 2 刻みあるだけで取り消していた (3436: 指値 3728 = 買気配、売気配 3730)。
        # BUY: 買い気配が指値より上へ離れたら (自分より高い買いが並んだ = 置いていかれた) 取消。
        if side_u == "BUY" and bid > 0 and bid >= px + tick * ticks:
            return True, "buy_bid_moved_away", detail
        # SELL: 売り気配が指値より下へ離れたら取消。
        if side_u == "SELL" and ask > 0 and ask <= px - tick * ticks:
            return True, "sell_ask_moved_away", detail
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
    intent = _take_order_intent(sym, side)
    if intent and cancel_after_sec is None:
        ca = float(intent["cancel_after_sec"])
    po = _ensure_pending_orders()
    po[oid] = {
        "mode": (intent or {}).get("mode"),
        "chase_round": (intent or {}).get("chase_round"),
        "ref_bid": (intent or {}).get("ref_bid"),
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


def _notify_summary_retry(symbol: str, order_id: str) -> None:
    """未約定キャンセル完了を SUMMARY 再queue 機構へ通知する。

    失敗してもキャンセル処理本体には影響させない。
    """
    try:
        from trading.handlers.entry_retry_rotation import on_unfilled_order_cancelled

        requeued = on_unfilled_order_cancelled(symbol, order_id)
        if requeued:
            logger.warning("[ENTRY CANCEL WATCH] summary retry requeued symbol=%s order_id=%s", symbol, order_id)
    except Exception:
        logger.debug("[ENTRY CANCEL WATCH] summary retry notify failed symbol=%s order_id=%s", symbol, order_id, exc_info=True)


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

    if info.get("mode") == "sell_wait":
        allow_retry = False
        logger.warning("[SELL WAIT] cancel order_id=%s symbol=%s reason=%s (追いかけない)", oid, symbol, reason)
    elif info.get("mode") == "buy_chase":
        # 離れたときだけ出し直す。回数の上限は entry_retry_rotation 側 (銘柄ごとの累積回数は使わない)。
        allow_retry = str(reason).startswith("entry_board_adverse")
        logger.warning("[BUY CHASE] cancel order_id=%s symbol=%s round=%s reason=%s retry=%s",
                       oid, symbol, info.get("chase_round"), reason, allow_retry)
    else:
        allow_retry = _mark_cancel_and_decide_retry(symbol, reason)
    if not allow_retry:
        _mark_symbol_cancel_cooldown(symbol, reason=reason)
    else:
        # 既存のリトライ判断が OK のときだけ、SUMMARY_AI 候補を
        # pending へ戻す (entry_retry_rotation 側でさらに上限回数を見る)。
        _notify_summary_retry(symbol, oid)

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

        if info.get("mode") == "sell_wait":
            # 2026-10-20: 空売りの 0.2% 上待ちは追いかけない。値が離れても待ち時間いっぱい置いておく
            if elapsed >= cancel_after:
                _cancel_pending_order(oid, info, "entry_unfilled_cancel_timeout", {"elapsed": elapsed, "cancel_after": cancel_after})
            continue
        ref_px = info.get("ref_bid") if info.get("mode") == "buy_chase" and info.get("ref_bid") else info.get("price")
        adverse, adverse_reason, adverse_detail = _is_board_adverse(symbol, str(info.get("side") or ""), ref_px)
        if adverse and info.get("mode") == "buy_chase":
            # 2026-10-20: 0.5 円刻みの銘柄は 1 円上がっただけで「2 刻み離れた」になる (3436: 2586.5 -> 2588 で 4 秒後に取消)。
            #   発注時の買気配から ENTRY_BUY_CHASE_AWAY_PCT (0.2%、下で待つ幅と同じ) 以上上がったときだけ「離れた」とする。
            away_pct = _env_float("ENTRY_BUY_CHASE_AWAY_PCT", 0.2)
            ref = float(info.get("ref_bid") or 0.0)
            bid_now = float((adverse_detail or {}).get("bid") or 0.0)
            if ref > 0 and bid_now < ref * (1 + away_pct / 100.0):
                adverse = False
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
