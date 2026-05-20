# ============================================================
# File   : core/startup/exit_board_profit_runtime_patch.py
# Version: Ver01-BOARD-AWARE-PROFIT-EXIT
# ------------------------------------------------------------
# 板を見て、利益が出る範囲で厚い板の1ティック手前に返済指値を置く。
# 約定しないまま逆行したら指値を取り消して、通常の成行/板タッチ返済へ戻す。
#
# 方針:
#   - 既存の process_exit 成行返済は残す
#   - 利益保護系 reason のときだけ board-aware 返済を試す
#   - 失敗時/逆行時/板なし時は既存 process_exit にフォールバック
#
# ENV:
#   EXIT_BOARD_PROFIT_ENABLED=1
#   EXIT_BOARD_PROFIT_REASONS=PROFIT_TAKE_FAST,PROFIT_PROTECT_FLOOR,PROFIT_PROTECT_GIVEBACK,NORMAL_TAKE_PROFIT,NORMAL_TRAIL,TONOSAMA_TRAIL,TONOSAMA_TAKE
#   EXIT_BOARD_MIN_PROFIT_PCT=0.0010
#   EXIT_BOARD_THICK_QTY_MULT=3.0
#   EXIT_BOARD_MIN_THICK_QTY=1000
#   EXIT_BOARD_MAX_WAIT_SEC=2.0
#   EXIT_BOARD_POLL_SEC=0.25
#   EXIT_BOARD_REVERSE_GAP_PCT=0.0010
# ============================================================

from __future__ import annotations

import json
import logging
import os
import time
from types import SimpleNamespace
from typing import Any

import requests

from token_manager import get_valid_token

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_PROCESS_EXIT = None
API_URL = "http://localhost:18080/kabusapi"


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
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


def _env_set(name: str, default: str) -> set[str]:
    try:
        raw = os.getenv(name, default)
        return {x.strip().upper() for x in str(raw).split(",") if x.strip()}
    except Exception:
        return {x.strip().upper() for x in default.split(",") if x.strip()}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _as_obj(pos: Any):
    return SimpleNamespace(**pos) if isinstance(pos, dict) else pos


def _symbol(pos: Any) -> str:
    return str(_get(pos, "symbol") or _get(pos, "Symbol") or _get(pos, "stock_code") or "").strip()


def _side(pos: Any) -> str:
    return str(_get(pos, "side") or _get(pos, "Side") or "BUY").upper()


def _qty(pos: Any) -> int:
    try:
        return int(float(_get(pos, "qty") or _get(pos, "quantity") or _get(pos, "Qty") or 0))
    except Exception:
        return 0


def _entry_price(pos: Any) -> float:
    return _safe_float(_get(pos, "avg_price") or _get(pos, "entry_price") or _get(pos, "price"), 0.0)


def _is_credit(pos: Any) -> bool:
    return _side(pos) in {"BUY_CREDIT", "SELL_CREDIT"}


def _is_buy_position(pos: Any) -> bool:
    return _side(pos) in {"BUY", "BUY_CREDIT"}


def _is_sell_position(pos: Any) -> bool:
    return _side(pos) in {"SELL", "SELL_CREDIT", "SHORT"}


def _exit_order_side(pos: Any) -> int:
    # kabu API: 1=売, 2=買
    if _is_buy_position(pos):
        return 1
    if _is_sell_position(pos):
        return 2
    return 0


def _pnl_rate(pos: Any, price: float) -> float:
    ep = _entry_price(pos)
    if ep <= 0 or price <= 0:
        return 0.0
    r = (price - ep) / ep
    if _is_sell_position(pos):
        r = -r
    return r


def _tick_size(price: float) -> float:
    # 東証の厳密な呼値単位とは完全一致ではないが、まず安全な簡易版。
    # 必要なら価格帯別に拡張する。
    if price < 1000:
        return 1.0
    if price < 3000:
        return 1.0
    if price < 5000:
        return 5.0
    if price < 30000:
        return 10.0
    if price < 50000:
        return 50.0
    return 100.0


def _round_to_tick(price: float) -> float:
    t = _tick_size(price)
    if t <= 0:
        return price
    return round(round(price / t) * t, 6)


def _latest_push_board(symbol: str) -> dict[str, Any]:
    try:
        from global_state import global_data
        df = None
        if hasattr(global_data, "get_push_df"):
            df = global_data.get_push_df()
        if df is None or getattr(df, "empty", True):
            return {}
        if "symbol" not in df.columns:
            return {}
        d = df[df["symbol"].astype(str) == str(symbol)]
        if d.empty:
            return {}
        row = d.iloc[-1].to_dict()
        return {str(k).lower(): v for k, v in row.items()}
    except Exception:
        logger.debug("[EXIT BOARD PROFIT] push board read failed symbol=%s", symbol, exc_info=True)
        return {}


def _price(row: dict, key: str) -> float:
    return _safe_float(row.get(key.lower()), 0.0)


def _board_levels(row: dict, side: str) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    prefix = "sell" if side == "sell" else "buy"
    for i in range(1, 11):
        p = _price(row, f"{prefix}{i}price")
        q = _price(row, f"{prefix}{i}qty")
        if p > 0 and q > 0:
            levels.append((p, q))
    return levels


def _choose_profit_limit_price(pos: Any, current_price: float) -> tuple[float, dict[str, Any]]:
    symbol = _symbol(pos)
    entry = _entry_price(pos)
    row = _latest_push_board(symbol)
    if not row:
        return 0.0, {"reason": "no_board"}

    tick = _tick_size(current_price or entry)
    min_profit = _env_float("EXIT_BOARD_MIN_PROFIT_PCT", 0.0010)
    thick_mult = _env_float("EXIT_BOARD_THICK_QTY_MULT", 3.0)
    min_thick = _env_float("EXIT_BOARD_MIN_THICK_QTY", 1000.0)

    current = _safe_float(row.get("price") or row.get("close") or current_price, current_price)
    bid1 = _price(row, "buy1price")
    ask1 = _price(row, "sell1price")

    if _is_buy_position(pos):
        # BUY建玉の返済は売り。利益方向は上。売り板の厚いところの1tick手前に売指値。
        levels = _board_levels(row, "sell")
        min_price = entry * (1.0 + min_profit)
        avg_qty = sum(q for _, q in levels) / len(levels) if levels else 0.0
        candidates = []
        for p, q in levels:
            limit_p = _round_to_tick(p - tick)
            if limit_p >= min_price and limit_p >= current:
                if q >= max(min_thick, avg_qty * thick_mult):
                    candidates.append((limit_p, p, q, avg_qty))
        if candidates:
            # 近い厚板を優先。遠すぎる利確待ちはしない。
            limit_p, thick_p, thick_q, avg_q = sorted(candidates, key=lambda x: x[0])[0]
            return limit_p, {"reason": "buy_position_sell_before_thick_ask", "thick_price": thick_p, "thick_qty": thick_q, "avg_qty": avg_q, "bid1": bid1, "ask1": ask1, "current": current}
        # 厚板が無い場合は現在値より1tick上、かつ最低利益以上だけ試す
        fallback = _round_to_tick(max(current + tick, min_price))
        return fallback, {"reason": "buy_position_fallback_profit_limit", "bid1": bid1, "ask1": ask1, "current": current}

    if _is_sell_position(pos):
        # SELL建玉の返済は買い。利益方向は下。買い板の厚いところの1tick手前に買指値。
        levels = _board_levels(row, "buy")
        max_price = entry * (1.0 - min_profit)
        avg_qty = sum(q for _, q in levels) / len(levels) if levels else 0.0
        candidates = []
        for p, q in levels:
            limit_p = _round_to_tick(p + tick)
            if limit_p <= max_price and limit_p <= current:
                if q >= max(min_thick, avg_qty * thick_mult):
                    candidates.append((limit_p, p, q, avg_qty))
        if candidates:
            # 近い厚板を優先。遠すぎる買戻し待ちはしない。
            limit_p, thick_p, thick_q, avg_q = sorted(candidates, key=lambda x: x[0], reverse=True)[0]
            return limit_p, {"reason": "sell_position_buy_before_thick_bid", "thick_price": thick_p, "thick_qty": thick_q, "avg_qty": avg_q, "bid1": bid1, "ask1": ask1, "current": current}
        fallback = _round_to_tick(min(current - tick, max_price))
        return fallback, {"reason": "sell_position_fallback_profit_limit", "bid1": bid1, "ask1": ask1, "current": current}

    return 0.0, {"reason": "unknown_side"}


def _conf_password() -> str:
    try:
        import configparser
        conf = configparser.ConfigParser()
        conf.read("settings.ini", encoding="utf-8")
        return conf.get("aukabu", "password", fallback="")
    except Exception:
        return ""


def _send_limit_close_order(pos: Any, limit_price: float) -> dict[str, Any] | None:
    token = get_valid_token()
    if not token:
        logger.error("[EXIT BOARD PROFIT] token missing")
        return None

    symbol = _symbol(pos)
    qty = _qty(pos)
    hold_id = _get(pos, "hold_id")
    side = _exit_order_side(pos)
    if not symbol or qty <= 0 or side not in {1, 2}:
        return None

    exchange = int(_get(pos, "exchange", 1) or 1)
    margin_type = int(_get(pos, "margin_trade_type", 1) or 1)
    account_type = int(_get(pos, "account_type", 4) or 4)

    body = {
        "Password": _conf_password(),
        "Symbol": str(symbol),
        "Exchange": exchange,
        "SecurityType": 1,
        "Side": str(side),
        "Qty": int(qty),
        "FrontOrderType": 20,  # 指値
        "Price": float(limit_price),
        "ExpireDay": 0,
    }

    if _is_credit(pos):
        if not hold_id:
            logger.warning("[EXIT BOARD PROFIT] credit position hold_id missing symbol=%s", symbol)
            return None
        body.update({
            "CashMargin": 3,
            "MarginTradeType": margin_type,
            "DelivType": 2,
            "AccountType": account_type,
            "ClosePositions": [{"HoldID": hold_id, "Qty": int(qty)}],
        })
    else:
        body.update({"CashMargin": 1})

    logger.warning("[EXIT BOARD PROFIT] SEND LIMIT CLOSE symbol=%s side=%s qty=%s limit=%s body=%s", symbol, _side(pos), qty, limit_price, body)

    try:
        res = requests.post(
            f"{API_URL}/sendorder",
            headers={"Content-Type": "application/json", "X-API-KEY": token},
            json=body,
            timeout=5,
        )
    except Exception as e:
        logger.warning("[EXIT BOARD PROFIT] limit close post failed symbol=%s err=%s", symbol, e, exc_info=False)
        return None

    if res.status_code != 200:
        logger.warning("[EXIT BOARD PROFIT] limit close rejected symbol=%s status=%s text=%s", symbol, res.status_code, res.text)
        return None

    js = res.json()
    oid = js.get("OrderId")
    if not oid:
        logger.warning("[EXIT BOARD PROFIT] limit close no OrderId symbol=%s res=%s", symbol, js)
        return None
    return {"order_id": oid, "limit_price": limit_price, "raw": js}


def _find_execution(order_id: str, token: str) -> dict[str, Any] | None:
    try:
        res = requests.get(
            f"{API_URL}/orders",
            headers={"X-API-KEY": token},
            params={"orderId": order_id},
            timeout=3,
        )
        if res.status_code != 200:
            return None
        arr = res.json()
        if not isinstance(arr, list):
            return None
        for od in arr:
            if str(od.get("OrderId")) != str(order_id):
                continue
            details = od.get("Details") or []
            for d in details:
                price = _safe_float(d.get("Price"), 0.0)
                qty = _safe_float(d.get("Qty"), 0.0)
                if price > 0 and qty > 0:
                    return {"price": price, "qty": qty, "exec_time": d.get("ExecutionDay")}
    except Exception:
        return None
    return None


def _cancel_order(order_id: str) -> bool:
    token = get_valid_token()
    if not token:
        return False
    body = {"Password": _conf_password(), "OrderId": str(order_id)}
    # kabuステーションAPIの取消は /cancelorder。環境差分に備えて失敗時はFalseで返す。
    try:
        res = requests.put(
            f"{API_URL}/cancelorder",
            headers={"Content-Type": "application/json", "X-API-KEY": token},
            json=body,
            timeout=3,
        )
        ok = res.status_code == 200
        logger.warning("[EXIT BOARD PROFIT] CANCEL order_id=%s ok=%s status=%s text=%s", order_id, ok, res.status_code, res.text[:300])
        return ok
    except Exception as e:
        logger.warning("[EXIT BOARD PROFIT] cancel failed order_id=%s err=%s", order_id, e, exc_info=False)
        return False


def _is_reverse(pos: Any, current_price: float, limit_price: float) -> bool:
    reverse_gap = _env_float("EXIT_BOARD_REVERSE_GAP_PCT", 0.0010)
    entry = _entry_price(pos)
    if entry <= 0 or current_price <= 0:
        return True

    # 利益が最低ラインを割ったら逆行扱い
    if _pnl_rate(pos, current_price) < _env_float("EXIT_BOARD_MIN_PROFIT_PCT", 0.0010):
        return True

    # BUY建玉: 売指値を置いた後、現在値が指値から下へ離れたら逆行
    if _is_buy_position(pos):
        return current_price <= limit_price * (1.0 - reverse_gap)

    # SELL建玉: 買指値を置いた後、現在値が指値から上へ離れたら逆行
    if _is_sell_position(pos):
        return current_price >= limit_price * (1.0 + reverse_gap)

    return True


def _current_board_price(symbol: str, fallback: float) -> float:
    row = _latest_push_board(symbol)
    return _safe_float(row.get("price") or row.get("close"), fallback)


def _board_exit_then_fallback(pos: Any, exit_price: float, reason: str) -> dict[str, Any] | None:
    if not callable(_ORIG_PROCESS_EXIT):
        return None

    if not _env_bool("EXIT_BOARD_PROFIT_ENABLED", True):
        return _ORIG_PROCESS_EXIT(_as_obj(pos), exit_price, reason)

    reasons = _env_set(
        "EXIT_BOARD_PROFIT_REASONS",
        "PROFIT_TAKE_FAST,PROFIT_PROTECT_FLOOR,PROFIT_PROTECT_GIVEBACK,NORMAL_TAKE_PROFIT,NORMAL_TRAIL,TONOSAMA_TRAIL,TONOSAMA_TAKE",
    )
    if str(reason or "").upper() not in reasons:
        return _ORIG_PROCESS_EXIT(_as_obj(pos), exit_price, reason)

    current = _current_board_price(_symbol(pos), exit_price)
    if _pnl_rate(pos, current) < _env_float("EXIT_BOARD_MIN_PROFIT_PCT", 0.0010):
        logger.warning("[EXIT BOARD PROFIT] skip board limit no enough profit symbol=%s pnl=%.4f reason=%s", _symbol(pos), _pnl_rate(pos, current), reason)
        return _ORIG_PROCESS_EXIT(_as_obj(pos), exit_price, reason)

    limit_price, diag = _choose_profit_limit_price(pos, current)
    if limit_price <= 0:
        logger.warning("[EXIT BOARD PROFIT] no limit price symbol=%s diag=%s -> fallback market", _symbol(pos), diag)
        return _ORIG_PROCESS_EXIT(_as_obj(pos), exit_price, reason)

    sent = _send_limit_close_order(pos, limit_price)
    if not sent:
        return _ORIG_PROCESS_EXIT(_as_obj(pos), exit_price, reason)

    order_id = sent["order_id"]
    token = get_valid_token()
    started = time.time()
    max_wait = _env_float("EXIT_BOARD_MAX_WAIT_SEC", 2.0)
    poll = max(0.05, _env_float("EXIT_BOARD_POLL_SEC", 0.25))

    logger.warning(
        "[EXIT BOARD PROFIT] WAIT symbol=%s order_id=%s limit=%s reason=%s diag=%s",
        _symbol(pos), order_id, limit_price, reason, diag,
    )

    while time.time() - started < max_wait:
        time.sleep(poll)
        if token:
            ex = _find_execution(order_id, token)
            if ex:
                logger.warning("[EXIT BOARD PROFIT] FILLED symbol=%s order_id=%s ex=%s", _symbol(pos), order_id, ex)
                return {"order_id": order_id, "exec_price": ex.get("price"), "exec_qty": ex.get("qty"), "exec_time": ex.get("exec_time"), "board_limit": True}

        cur = _current_board_price(_symbol(pos), current)
        if _is_reverse(pos, cur, limit_price):
            logger.warning(
                "[EXIT BOARD PROFIT] REVERSE cancel+fallback symbol=%s order_id=%s cur=%s limit=%s pnl=%.4f reason=%s",
                _symbol(pos), order_id, cur, limit_price, _pnl_rate(pos, cur), reason,
            )
            _cancel_order(order_id)
            return _ORIG_PROCESS_EXIT(_as_obj(pos), cur, reason + "_BOARD_REVERSE_FALLBACK")

    logger.warning("[EXIT BOARD PROFIT] TIMEOUT cancel+fallback symbol=%s order_id=%s", _symbol(pos), order_id)
    _cancel_order(order_id)
    cur = _current_board_price(_symbol(pos), current)
    return _ORIG_PROCESS_EXIT(_as_obj(pos), cur, reason + "_BOARD_TIMEOUT_FALLBACK")


def _patched_process_exit(position: Any, exit_price: float, reason: str):
    try:
        return _board_exit_then_fallback(position, exit_price, reason)
    except Exception as e:
        logger.warning("[EXIT BOARD PROFIT] patched process_exit failed symbol=%s err=%s -> fallback", _symbol(position), e, exc_info=False)
        if callable(_ORIG_PROCESS_EXIT):
            return _ORIG_PROCESS_EXIT(_as_obj(position), exit_price, reason)
        return None


def install() -> bool:
    global _INSTALLED, _ORIG_PROCESS_EXIT
    if _INSTALLED:
        return True

    try:
        import kabu_api.close as close_mod
        import trading.handlers.exit_handler as exit_handler

        cur = getattr(close_mod, "process_exit", None)
        if getattr(cur, "_exit_board_profit_patch_v1", False):
            _INSTALLED = True
            return True

        _ORIG_PROCESS_EXIT = cur
        _patched_process_exit._exit_board_profit_patch_v1 = True  # type: ignore[attr-defined]

        close_mod.process_exit = _patched_process_exit
        # exit_handler は from kabu_api.close import process_exit で参照を持っているため差し替える
        exit_handler.process_exit = _patched_process_exit

        _INSTALLED = True
        logger.warning(
            "[EXIT BOARD PROFIT] installed enabled=%s min_profit=%.4f thick_mult=%.2f min_thick=%s wait=%.2fs reverse_gap=%.4f",
            _env_bool("EXIT_BOARD_PROFIT_ENABLED", True),
            _env_float("EXIT_BOARD_MIN_PROFIT_PCT", 0.0010),
            _env_float("EXIT_BOARD_THICK_QTY_MULT", 3.0),
            _env_int("EXIT_BOARD_MIN_THICK_QTY", 1000),
            _env_float("EXIT_BOARD_MAX_WAIT_SEC", 2.0),
            _env_float("EXIT_BOARD_REVERSE_GAP_PCT", 0.0010),
        )
        return True
    except Exception as e:
        logger.exception("[EXIT BOARD PROFIT] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[EXIT BOARD PROFIT] auto install failed err=%s", e)

__all__ = ["install"]
