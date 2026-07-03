# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)
VERSION = "V2-EXIT-NO-70PCT-PROGRESS-5MIN-FAVORABLE-THEN-MARKET"
_INSTALLED = False
_THREAD_STARTED = False
_STATES: dict[str, dict[str, Any]] = {}

API_URL = "http://localhost:18080/kabusapi"


def _env_bool(name: str, default: bool) -> bool:
    try:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(str(raw).replace(",", "")))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _position_key(p: dict[str, Any]) -> str:
    return "|".join(str(x or "") for x in (
        _norm_symbol(p.get("Symbol")),
        p.get("ExecutionID") or p.get("HoldID"),
        p.get("Side"),
    ))


def _close_side_from_position(p: dict[str, Any]) -> int:
    # kabu /positions Side: 1=売建, 2=買建. Close side is the opposite.
    return 2 if str(p.get("Side") or "").strip() == "1" else 1


def _is_short_position(p: dict[str, Any]) -> bool:
    return str(p.get("Side") or "").strip() == "1"


def _is_credit_position(p: dict[str, Any]) -> bool:
    try:
        if not isinstance(p, dict):
            return False
        if _safe_int(p.get("MarginTradeType"), 0) <= 0:
            return False
        if _safe_int(p.get("LeavesQty") or p.get("Qty"), 0) <= 0:
            return False
        if not _norm_symbol(p.get("Symbol")):
            return False
        if not (p.get("ExecutionID") or p.get("HoldID")):
            return False
        return True
    except Exception:
        return False


def _get_bid_ask(symbol: str) -> tuple[float, float]:
    try:
        from utils_common import get_latest_bid_ask
        q = get_latest_bid_ask(symbol)
        if isinstance(q, dict):
            return _safe_float(q.get("bid_price") or q.get("bid") or q.get("best_bid"), 0.0), _safe_float(q.get("ask_price") or q.get("ask") or q.get("best_ask"), 0.0)
    except Exception:
        logger.debug("[EXIT NO 70PCT PROGRESS] bid/ask fetch failed symbol=%s", symbol, exc_info=True)
    return 0.0, 0.0


def _current_price(symbol: str, p: dict[str, Any]) -> float:
    bid, ask = _get_bid_ask(symbol)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if bid > 0:
        return bid
    if ask > 0:
        return ask
    for k in ("CurrentPrice", "Current", "Price", "price", "current_price", "MarketPrice"):
        px = _safe_float(p.get(k), 0.0)
        if px > 0:
            return px
    return 0.0


def _entry_price(p: dict[str, Any], current_px: float) -> float:
    for k in ("Price", "AvgPrice", "AveragePrice", "ExecutionPrice", "EntryPrice", "price", "avg_price"):
        px = _safe_float(p.get(k), 0.0)
        if px > 0:
            return px
    return current_px


def _favorable_limit_price(symbol: str, close_side: int) -> float:
    bid, ask = _get_bid_ask(symbol)
    # Long close is SELL: try ask. Short close is BUY: try bid.
    if close_side == 1:
        return ask if ask > 0 else bid
    return bid if bid > 0 else ask


def _send_credit_close_limit_order(symbol: str, qty: int, hold_id: Any, side: int, exchange: int, margin_type: int, account_type: int, price: float) -> dict[str, Any] | None:
    try:
        from token_manager import get_valid_token
        import configparser

        token = get_valid_token()
        if not token:
            logger.error("[EXIT NO 70PCT PROGRESS] LIMIT close token unavailable symbol=%s", symbol)
            return None
        conf = configparser.ConfigParser()
        conf.read("settings.ini", encoding="utf-8")
        password = conf.get("aukabu", "password", fallback="")
        body = {
            "Password": password,
            "Symbol": str(symbol),
            "Exchange": int(exchange or _env_int("ENTRY_ORDER_EXCHANGE", 9)),
            "SecurityType": 1,
            "Side": int(side),
            "CashMargin": 3,
            "MarginTradeType": int(margin_type or _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)),
            "DelivType": 2,
            "AccountType": int(account_type or 4),
            "Qty": int(qty),
            "FrontOrderType": 20,
            "Price": float(price),
            "ExpireDay": 0,
            "ClosePositions": [{"HoldID": hold_id, "Qty": int(qty)}],
        }
        logger.warning("[EXIT NO 70PCT PROGRESS] FAVORABLE LIMIT SEND symbol=%s close_side=%s price=%s qty=%s hold_id=%s body=%s", symbol, side, price, qty, hold_id, json.dumps(body, ensure_ascii=False))
        res = requests.post(f"{API_URL}/sendorder", headers={"Content-Type": "application/json", "X-API-KEY": token}, json=body, timeout=5)
        try:
            data: Any = res.json()
        except Exception:
            data = res.text
        if res.status_code != 200 or not isinstance(data, dict) or not data.get("OrderId"):
            logger.warning("[EXIT NO 70PCT PROGRESS] FAVORABLE LIMIT NG symbol=%s status=%s response=%s", symbol, res.status_code, data)
            return None
        logger.warning("[EXIT NO 70PCT PROGRESS] FAVORABLE LIMIT SENT symbol=%s order_id=%s price=%s qty=%s", symbol, data.get("OrderId"), price, qty)
        return {"order_id": data.get("OrderId"), "price": price, "qty": qty}
    except Exception:
        logger.exception("[EXIT NO 70PCT PROGRESS] FAVORABLE LIMIT exception symbol=%s", symbol)
        return None


def _position_still_open(key: str) -> bool:
    try:
        from kabu_api.positions import get_positions
        positions = get_positions() or []
        if not isinstance(positions, list):
            return True
        for p in positions:
            if isinstance(p, dict) and _is_credit_position(p) and _position_key(p) == key:
                return True
        return False
    except Exception:
        logger.debug("[EXIT NO 70PCT PROGRESS] position recheck failed key=%s", key, exc_info=True)
        return True


def _market_close_position(p: dict[str, Any], reason: str) -> bool:
    try:
        import kabu_api.close as close_mod
        symbol = _norm_symbol(p.get("Symbol"))
        qty = _safe_int(p.get("LeavesQty") or p.get("Qty"), 0)
        hold_id = p.get("ExecutionID") or p.get("HoldID")
        side = _close_side_from_position(p)
        exchange = _safe_int(p.get("Exchange"), _env_int("ENTRY_ORDER_EXCHANGE", 9)) or _env_int("ENTRY_ORDER_EXCHANGE", 9)
        margin_type = _safe_int(p.get("MarginTradeType"), _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)) or _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)
        account_type = _safe_int(p.get("AccountType"), 4) or 4
        logger.warning("[EXIT NO 70PCT PROGRESS] MARKET FALLBACK SEND symbol=%s side=%s qty=%s hold_id=%s reason=%s", symbol, side, qty, hold_id, reason)
        res = close_mod.send_credit_close_order(symbol, qty, hold_id, side, exchange, margin_type, account_type)
        ok = bool(res and res.get("order_id"))
        logger.warning("[EXIT NO 70PCT PROGRESS] MARKET FALLBACK done symbol=%s ok=%s res=%s", symbol, ok, res)
        return ok
    except Exception:
        logger.exception("[EXIT NO 70PCT PROGRESS] MARKET FALLBACK exception")
        return False


def _exit_favorable_then_market(p: dict[str, Any], state: dict[str, Any], reason: str) -> None:
    symbol = _norm_symbol(p.get("Symbol"))
    key = _position_key(p)
    if state.get("exit_sent"):
        return
    state["exit_sent"] = True
    qty = _safe_int(p.get("LeavesQty") or p.get("Qty"), 0)
    hold_id = p.get("ExecutionID") or p.get("HoldID")
    close_side = _close_side_from_position(p)
    exchange = _safe_int(p.get("Exchange"), _env_int("ENTRY_ORDER_EXCHANGE", 9)) or _env_int("ENTRY_ORDER_EXCHANGE", 9)
    margin_type = _safe_int(p.get("MarginTradeType"), _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)) or _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)
    account_type = _safe_int(p.get("AccountType"), 4) or 4
    price = _favorable_limit_price(symbol, close_side)
    if price <= 0 or qty <= 0 or not hold_id:
        logger.warning("[EXIT NO 70PCT PROGRESS] favorable skipped -> market symbol=%s price=%s qty=%s hold_id=%s", symbol, price, qty, hold_id)
        _market_close_position(p, reason + ":no_favorable_price")
        return
    limit_res = _send_credit_close_limit_order(symbol, qty, hold_id, close_side, exchange, margin_type, account_type, price)
    state["favorable_order_id"] = limit_res.get("order_id") if isinstance(limit_res, dict) else None
    wait_sec = max(0.5, _env_float("EXIT_NO_PROGRESS_LIMIT_WAIT_SEC", _env_float("EXIT_STAGNATION_LIMIT_WAIT_SEC", 3.0)))
    time.sleep(wait_sec)
    if not _position_still_open(key):
        logger.warning("[EXIT NO 70PCT PROGRESS] favorable filled/position gone symbol=%s order_id=%s", symbol, state.get("favorable_order_id"))
        return
    if state.get("favorable_order_id"):
        try:
            from kabu_api.cancel_order import cancel_order_common
            cancel_order_common(str(state.get("favorable_order_id")), symbol=symbol, reason="exit_no_70pct_progress_limit_unfilled_market_fallback")
        except Exception:
            logger.debug("[EXIT NO 70PCT PROGRESS] cancel favorable failed symbol=%s", symbol, exc_info=True)
    _market_close_position(p, reason + ":limit_unfilled_market_fallback")


def _update_progress_state(symbol: str, p: dict[str, Any], state: dict[str, Any], px: float) -> None:
    entry_px = float(state.get("entry_price") or 0.0)
    if entry_px <= 0 or px <= 0:
        return
    take_pct = abs(_env_float("EXIT_NO_PROGRESS_TAKE_PROFIT_PCT", _env_float("EXIT_TAKE_PROFIT_PCT", 0.0020)))
    stop_pct = abs(_env_float("EXIT_NO_PROGRESS_STOP_LOSS_PCT", _env_float("EXIT_STOP_LOSS_PCT", 0.0030)))
    ratio = max(0.01, min(_env_float("EXIT_NO_PROGRESS_THRESHOLD_RATIO", 0.70), 1.0))
    take70 = take_pct * ratio
    stop70 = stop_pct * ratio
    if _is_short_position(p):
        favorable = (entry_px - px) / entry_px
        adverse = (px - entry_px) / entry_px
    else:
        favorable = (px - entry_px) / entry_px
        adverse = (entry_px - px) / entry_px
    state["max_favorable_pct"] = max(float(state.get("max_favorable_pct") or 0.0), favorable)
    state["max_adverse_pct"] = max(float(state.get("max_adverse_pct") or 0.0), adverse)
    if favorable >= take70:
        state["reached_take70"] = True
    if adverse >= stop70:
        state["reached_stop70"] = True
    state["take70_pct"] = take70
    state["stop70_pct"] = stop70


def _monitor_loop() -> None:
    poll = max(0.5, _env_float("EXIT_NO_PROGRESS_POLL_SEC", _env_float("EXIT_STAGNATION_POLL_SEC", 1.0)))
    window_sec = max(30.0, _env_float("EXIT_NO_PROGRESS_WINDOW_SEC", 300.0))
    logger.warning("[EXIT NO 70PCT PROGRESS] loop started poll=%.2fs window=%.1fs ratio=%s version=%s", poll, window_sec, os.environ.get("EXIT_NO_PROGRESS_THRESHOLD_RATIO", "0.70"), VERSION)
    while True:
        try:
            if not _env_bool("EXIT_NO_PROGRESS_ENABLED", _env_bool("EXIT_STAGNATION_ENABLED", True)):
                time.sleep(5.0)
                continue
            from kabu_api.positions import get_positions
            positions = get_positions() or []
            now = time.time()
            live_keys: set[str] = set()
            if isinstance(positions, list):
                for p in positions:
                    if not _is_credit_position(p):
                        continue
                    symbol = _norm_symbol(p.get("Symbol"))
                    key = _position_key(p)
                    live_keys.add(key)
                    px = _current_price(symbol, p)
                    if px <= 0:
                        continue
                    state = _STATES.get(key)
                    if state is None:
                        ep = _entry_price(p, px)
                        state = {
                            "first_seen": now,
                            "entry_price": ep,
                            "reached_take70": False,
                            "reached_stop70": False,
                            "max_favorable_pct": 0.0,
                            "max_adverse_pct": 0.0,
                            "exit_sent": False,
                        }
                        _STATES[key] = state
                        logger.warning("[EXIT NO 70PCT PROGRESS] tracking start symbol=%s entry_price=%s current=%s window=%.1fs", symbol, ep, px, window_sec)
                    _update_progress_state(symbol, p, state, px)
                    age = now - float(state.get("first_seen") or now)
                    if age >= window_sec and not state.get("exit_sent"):
                        reached_take = bool(state.get("reached_take70"))
                        reached_stop = bool(state.get("reached_stop70"))
                        if not reached_take and not reached_stop:
                            logger.warning(
                                "[EXIT NO 70PCT PROGRESS] detected symbol=%s age=%.1fs entry=%s current=%s max_fav=%.5f max_adv=%.5f take70=%.5f stop70=%.5f -> favorable then market",
                                symbol,
                                age,
                                state.get("entry_price"),
                                px,
                                float(state.get("max_favorable_pct") or 0.0),
                                float(state.get("max_adverse_pct") or 0.0),
                                float(state.get("take70_pct") or 0.0),
                                float(state.get("stop70_pct") or 0.0),
                            )
                            threading.Thread(target=_exit_favorable_then_market, args=(dict(p), state, "no_70pct_progress_5min"), name=f"exit-no-progress-{symbol}", daemon=True).start()
                        else:
                            state["exit_sent"] = True
                            logger.warning("[EXIT NO 70PCT PROGRESS] pass symbol=%s age=%.1fs reached_take70=%s reached_stop70=%s max_fav=%.5f max_adv=%.5f", symbol, age, reached_take, reached_stop, float(state.get("max_favorable_pct") or 0.0), float(state.get("max_adverse_pct") or 0.0))
            for k in list(_STATES.keys()):
                if k not in live_keys:
                    _STATES.pop(k, None)
            time.sleep(poll)
        except Exception:
            logger.exception("[EXIT NO 70PCT PROGRESS] loop error")
            time.sleep(3.0)


def install() -> bool:
    global _INSTALLED, _THREAD_STARTED
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("EXIT_NO_PROGRESS_ENABLED", "1")
        os.environ.setdefault("EXIT_NO_PROGRESS_POLL_SEC", "1.0")
        os.environ.setdefault("EXIT_NO_PROGRESS_WINDOW_SEC", "300.0")
        os.environ.setdefault("EXIT_NO_PROGRESS_THRESHOLD_RATIO", "0.70")
        os.environ.setdefault("EXIT_NO_PROGRESS_TAKE_PROFIT_PCT", os.environ.get("EXIT_TAKE_PROFIT_PCT", "0.0020"))
        os.environ.setdefault("EXIT_NO_PROGRESS_STOP_LOSS_PCT", os.environ.get("EXIT_STOP_LOSS_PCT", "0.0030"))
        os.environ.setdefault("EXIT_NO_PROGRESS_LIMIT_WAIT_SEC", "3.0")
        if not _THREAD_STARTED and _env_bool("EXIT_NO_PROGRESS_ENABLED", True):
            th = threading.Thread(target=_monitor_loop, name="exit-no-70pct-progress-monitor", daemon=True)
            th.start()
            _THREAD_STARTED = True
        _INSTALLED = True
        logger.warning("[EXIT NO 70PCT PROGRESS] installed version=%s enabled=%s window=%s ratio=%s take_pct=%s stop_pct=%s limit_wait=%s", VERSION, os.environ.get("EXIT_NO_PROGRESS_ENABLED"), os.environ.get("EXIT_NO_PROGRESS_WINDOW_SEC"), os.environ.get("EXIT_NO_PROGRESS_THRESHOLD_RATIO"), os.environ.get("EXIT_NO_PROGRESS_TAKE_PROFIT_PCT"), os.environ.get("EXIT_NO_PROGRESS_STOP_LOSS_PCT"), os.environ.get("EXIT_NO_PROGRESS_LIMIT_WAIT_SEC"))
        return True
    except Exception:
        logger.exception("[EXIT NO 70PCT PROGRESS] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[EXIT NO 70PCT PROGRESS] auto install failed")


__all__ = ["install", "VERSION"]
