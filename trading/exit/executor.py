# ============================================================
# File   : trading/exit/executor.py
# Version: Ver29.0-DB-OPEN-POSITION-FALLBACK
# ------------------------------------------------------------
# EXIT最終実行層。
#
# Ver29.0:
#   - EXIT価格取得と条件判定は通っているのに、executor側で
#       [EXIT_EXECUTOR] no open position symbol=9716
#     となり返済実行前に止まる問題を修正。
#   - global_data.open_positions / GC.positions に無い場合、positions.db の
#     OPEN建玉を直接fallback取得する。
#   - 取得したDB建玉は global_data にも同期し、以降の二重EXIT防止にも使う。
#   - 返済注文成功後だけDBをCLOSEDに更新する方針は維持。
# ============================================================

from __future__ import annotations

import configparser
import datetime as dt
import json
import logging
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from core.global_context.context import global_context as GC
from database import Session_position
from database.models import Position
from global_state import global_data
from kabu_api.send_order import send_order_common

logger = logging.getLogger(__name__)

_exit_lock = threading.Lock()
_EXIT_ORDER_SENDER: Optional[Callable[..., Any]] = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
        return True
    if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}:
        return False
    return bool(default)


EXIT_REQUIRE_ORDER_SUCCESS = _env_bool("EXIT_REQUIRE_ORDER_SUCCESS", True)
EXIT_DRY_RUN_ORDER_SUCCESS = _env_bool("EXIT_DRY_RUN_ORDER_SUCCESS", False)

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")


def set_exit_order_sender(sender: Optional[Callable[..., Any]]) -> None:
    global _EXIT_ORDER_SENDER
    if sender is not None and not callable(sender):
        raise TypeError("sender must be callable or None")
    _EXIT_ORDER_SENDER = sender
    logger.info(
        "[EXIT_EXECUTOR] exit order sender set sender=%s",
        getattr(sender, "__name__", type(sender).__name__) if sender else None,
    )


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
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


def _close_side_from_position_side(side: str) -> str:
    side = _normalize_side(side)
    if side == "BUY":
        return "SELL"
    if side == "SELL":
        return "BUY"
    return ""


def _position_to_dict(pos: Any) -> dict[str, Any]:
    if pos is None:
        return {}
    if isinstance(pos, dict):
        return dict(pos)

    out: dict[str, Any] = {}
    for name in [
        "id",
        "symbol",
        "symbolname",
        "side",
        "qty",
        "quantity",
        "avg_price",
        "entry_price",
        "price",
        "current_price",
        "entry_time",
        "created_at",
        "updated_at",
        "status",
        "atr",
        "atr_1min",
        "order_id",
        "entry_order_id",
        "exchange",
        "margin_trade_type",
        "account_type",
        "hold_id",
        "execution_id",
    ]:
        try:
            if hasattr(pos, name):
                out[name] = getattr(pos, name)
        except Exception:
            pass
    return out


def _snapshot_gc_positions() -> dict[str, Any]:
    try:
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is None:
            return {}
        for method_name in ["snapshot_open", "snapshot_dict", "get_open_positions", "to_dict"]:
            fn = getattr(positions_obj, method_name, None)
            if callable(fn):
                try:
                    ret = fn() or {}
                    if isinstance(ret, dict) and ret:
                        return ret
                except Exception:
                    logger.debug("[EXIT_EXECUTOR] GC.positions.%s failed", method_name, exc_info=True)
        for attr in ["open_positions", "positions"]:
            ret = getattr(positions_obj, attr, None)
            if isinstance(ret, dict) and ret:
                return ret
    except Exception:
        logger.debug("[EXIT_EXECUTOR] GC.positions snapshot failed", exc_info=True)
    return {}


def _ensure_global_open_positions() -> dict[str, Any]:
    try:
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            return positions
        positions = {}
        setattr(global_data, "open_positions", positions)
        return positions
    except Exception:
        return {}


def _load_db_open_position(symbol: str) -> Optional[dict[str, Any]]:
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return None

    session = None
    try:
        session = Session_position()
        rows = (
            session.query(Position)
            .filter(Position.symbol == symbol)
            .all()
        )
        closed = {"CLOSED", "CLOSE", "EXITED", "EXIT", "DONE", "CANCELED", "CANCELLED", "REJECTED", "FAILED"}
        best: Optional[dict[str, Any]] = None
        for p in rows or []:
            status = str(getattr(p, "status", "") or "OPEN").strip().upper()
            if status in closed:
                continue
            d = _position_to_dict(p)
            d.setdefault("symbol", symbol)
            d["symbol"] = _normalize_symbol(d.get("symbol") or symbol)
            d["side"] = _normalize_side(d.get("side"))
            d["qty"] = _safe_float(d.get("qty") or d.get("quantity"), 0.0)
            d["quantity"] = d["qty"]
            avg = _safe_float(d.get("avg_price"), 0.0)
            ent = _safe_float(d.get("entry_price"), 0.0)
            price = _safe_float(d.get("price") or d.get("current_price"), 0.0)
            if avg <= 0:
                avg = ent if ent > 0 else price
            if ent <= 0:
                ent = avg if avg > 0 else price
            d["avg_price"] = avg
            d["entry_price"] = ent
            d.setdefault("exchange", getattr(p, "exchange", None) or 1)
            d.setdefault("status", status or "OPEN")
            d["_position_source"] = "DB.positions.executor_fallback"
            if d["qty"] > 0 and d["entry_price"] > 0:
                best = d
                break
        if best:
            logger.warning(
                "[EXIT_EXECUTOR] DB open position fallback hit symbol=%s qty=%s side=%s entry=%.4f",
                symbol,
                best.get("qty"),
                best.get("side"),
                _safe_float(best.get("entry_price")),
            )
            return best
    except Exception:
        logger.debug("[EXIT_EXECUTOR] DB open position fallback failed symbol=%s", symbol, exc_info=True)
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass
    return None


def _get_open_position(symbol: str) -> tuple[Optional[dict[str, Any]], str]:
    symbol = _normalize_symbol(symbol)

    # 1. global_data.open_positions
    try:
        gd_positions = _ensure_global_open_positions()
        pos = gd_positions.get(symbol) or gd_positions.get(str(symbol))
        if pos is not None:
            d = _position_to_dict(pos)
            if d:
                d.setdefault("symbol", symbol)
                gd_positions[symbol] = d
                return d, "global_data.open_positions"
    except Exception:
        logger.debug("[EXIT_EXECUTOR] global_data position lookup failed symbol=%s", symbol, exc_info=True)

    # 2. GC.positions fallback
    try:
        gc_positions = _snapshot_gc_positions()
        pos = gc_positions.get(symbol) or gc_positions.get(str(symbol))
        if pos is not None:
            d = _position_to_dict(pos)
            if d:
                d.setdefault("symbol", symbol)
                gd_positions = _ensure_global_open_positions()
                gd_positions[symbol] = d
                logger.warning("[EXIT_EXECUTOR] position recovered from GC.positions symbol=%s", symbol)
                return d, "GC.positions"
    except Exception:
        logger.debug("[EXIT_EXECUTOR] GC position lookup failed symbol=%s", symbol, exc_info=True)

    # 3. positions.db fallback。EXIT判定がDB建玉を見ているのにexecutorがメモリだけで止まる問題を防ぐ。
    db_pos = _load_db_open_position(symbol)
    if db_pos:
        gd_positions = _ensure_global_open_positions()
        gd_positions[symbol] = db_pos
        try:
            positions_obj = getattr(GC, "positions", None)
            if positions_obj is not None:
                for method_name in ["set", "put", "add", "upsert", "update_position"]:
                    fn = getattr(positions_obj, method_name, None)
                    if callable(fn):
                        try:
                            fn(symbol, db_pos)
                            break
                        except TypeError:
                            try:
                                fn(db_pos)
                                break
                            except Exception:
                                pass
                        except Exception:
                            pass
        except Exception:
            pass
        return db_pos, "DB.positions.executor_fallback"

    return None, ""


def _parse_order_result(ret: Any) -> tuple[bool, str, str]:
    if ret is True:
        return True, "", "true"
    if ret is False or ret is None:
        return False, "", str(ret)
    if isinstance(ret, dict):
        ok = bool(ret.get("ok") or ret.get("success") or ret.get("result") or ret.get("accepted") or ret.get("OrderId") or ret.get("order_id"))
        order_id = str(ret.get("order_id") or ret.get("orderId") or ret.get("OrderId") or ret.get("id") or "")
        message = str(ret.get("message") or ret.get("msg") or ret.get("error") or ret.get("reason") or ret)
        return ok, order_id, message
    ok_attr = getattr(ret, "ok", None)
    success_attr = getattr(ret, "success", None)
    accepted_attr = getattr(ret, "accepted", None)
    if ok_attr is not None or success_attr is not None or accepted_attr is not None:
        ok = bool(ok_attr or success_attr or accepted_attr)
        order_id = str(getattr(ret, "order_id", "") or getattr(ret, "orderId", "") or getattr(ret, "OrderId", "") or getattr(ret, "id", "") or "")
        return ok, order_id, str(ret)
    return False, "", str(ret)


def _get_injected_sender() -> Optional[Callable[..., Any]]:
    if _EXIT_ORDER_SENDER is not None:
        return _EXIT_ORDER_SENDER
    for attr in ["exit_order_sender", "close_position_sender", "send_exit_order"]:
        try:
            fn = getattr(global_data, attr, None)
            if callable(fn):
                return fn
        except Exception:
            pass
    return None


# ============================================================
# EXIT 板タッチ指値
# (旧 core/startup/exit_limit_board_touch_runtime_patch.py V1.1 から移設)
# ------------------------------------------------------------
# EXIT返済注文を成行固定から板タッチ指値へ変更する。
#   BUY建玉返済  close_side=SELL -> 原則 bid 指値
#   SELL建玉返済 close_side=BUY  -> 原則 ask 指値
# REST /board が使える場合は複数段の板を見て厚い板側へ寄せる。
# 取れない場合は PUSH の最良 bid/ask へ、それも無ければ既定で成行へ戻る。
#
# 旧パッチは core/startup/__init__.py 経由でのみ install されていたため
# DB専用子プロセスでは適用されなかった。本文化後も
# _exit_board_runtime_enabled() で同じスコープを保つ。
# ============================================================

_EXIT_BOARD_CACHE: dict[str, tuple[float, dict]] = {}
_EXIT_BOARD_LAST_CALL: dict[str, float] = {}

_EXIT_BOARD_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def _exit_board_env_bool(name: str, default: bool) -> bool:
    """旧パッチと同じ意味の env 判定 (未設定/空文字は default、それ以外は真値集合で判定)。"""
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _exit_board_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _exit_board_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        return float(default) if math.isnan(x) or math.isinf(x) else x
    except Exception:
        return float(default)


def _exit_board_runtime_enabled() -> bool:
    """このプロセスで板タッチ指値を適用するか。

    EXIT_LIMIT_BOARD_TOUCH_SCOPE_ENABLED=1/0 で明示的に上書きできる。
    CORE_STARTUP_FORCE_FULL_INIT_PATCHES=1 のときは DB プロセスでも適用する。
    """
    try:
        override = os.getenv("EXIT_LIMIT_BOARD_TOUCH_SCOPE_ENABLED")
        if override is not None and str(override).strip() != "":
            return _exit_board_env_bool("EXIT_LIMIT_BOARD_TOUCH_SCOPE_ENABLED", True)
        if _exit_board_env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv_text for name in _EXIT_BOARD_DB_PROCESS_NAMES)
    except Exception:
        return True


def _exit_board_get_bid_ask(symbol: str) -> tuple[float, float]:
    try:
        from utils_common import get_latest_bid_ask
        ret = get_latest_bid_ask(str(symbol))
        if isinstance(ret, dict):
            bid = _exit_board_float(ret.get("bid") or ret.get("bid_price") or ret.get("BidPrice") or ret.get("best_bid"), 0.0)
            ask = _exit_board_float(ret.get("ask") or ret.get("ask_price") or ret.get("AskPrice") or ret.get("best_ask"), 0.0)
            return bid, ask
        if isinstance(ret, (tuple, list)) and len(ret) >= 2:
            return _exit_board_float(ret[0], 0.0), _exit_board_float(ret[1], 0.0)
    except Exception:
        logger.debug("[EXIT LIMIT BOARD TOUCH] utils_common.get_latest_bid_ask failed symbol=%s", symbol, exc_info=True)

    try:
        tick = global_data.get_latest_tick(str(symbol))
        if isinstance(tick, dict):
            bid = _exit_board_float(tick.get("BidPrice") or tick.get("bid") or tick.get("best_bid"), 0.0)
            ask = _exit_board_float(tick.get("AskPrice") or tick.get("ask") or tick.get("best_ask"), 0.0)
            return bid, ask
    except Exception:
        logger.debug("[EXIT LIMIT BOARD TOUCH] global tick bid/ask failed symbol=%s", symbol, exc_info=True)

    return 0.0, 0.0


def _exit_board_get_token() -> Optional[str]:
    try:
        from token_manager import get_valid_token, refresh_token
        return get_valid_token() or refresh_token()
    except Exception:
        logger.debug("[EXIT REST FULL BOARD] token unavailable", exc_info=True)
        return None


def _exit_board_fetch_rest(symbol: str, exchange: int) -> Optional[dict]:
    if not _exit_board_env_bool("EXIT_REST_FULL_BOARD_ENABLED", True):
        return None

    now = time.monotonic()
    cache_sec = _exit_board_env_float("EXIT_REST_FULL_BOARD_CACHE_SEC", 0.8)
    cached = _EXIT_BOARD_CACHE.get(symbol)
    if cached and now - cached[0] <= cache_sec:
        out = dict(cached[1])
        out["cache_hit"] = True
        return out

    min_interval = _exit_board_env_float("EXIT_REST_FULL_BOARD_MIN_INTERVAL_SEC", 0.7)
    if cached and now - _EXIT_BOARD_LAST_CALL.get(symbol, 0.0) < min_interval:
        out = dict(cached[1])
        out["cache_hit"] = True
        out["rate_limited_cache"] = True
        return out

    token = _exit_board_get_token()
    if not token:
        return None

    base = os.getenv("KABUSAPI_BASE_URL", "http://localhost:18080/kabusapi").rstrip("/")
    ex = str(os.getenv("EXIT_REST_FULL_BOARD_EXCHANGE", str(exchange or 1)) or exchange or 1)
    url = f"{base}/board/{urllib.parse.quote(str(symbol) + '@' + ex)}"
    req = urllib.request.Request(url, headers={"X-API-KEY": token}, method="GET")
    _EXIT_BOARD_LAST_CALL[symbol] = now
    try:
        with urllib.request.urlopen(req, timeout=_exit_board_env_float("EXIT_REST_FULL_BOARD_TIMEOUT_SEC", 0.8)) as res:
            raw = json.loads(res.read().decode("utf-8", errors="replace"))
    except Exception:
        logger.debug("[EXIT REST FULL BOARD] request failed symbol=%s", symbol, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None

    depth = int(_exit_board_env_float("EXIT_REST_FULL_BOARD_DEPTH", 10))
    bids, asks = [], []
    for i in range(1, max(1, depth) + 1):
        b = raw.get(f"Buy{i}") if isinstance(raw.get(f"Buy{i}"), dict) else {}
        a = raw.get(f"Sell{i}") if isinstance(raw.get(f"Sell{i}"), dict) else {}
        bp, bq = _exit_board_float(b.get("Price")), _exit_board_float(b.get("Qty"))
        ap, aq = _exit_board_float(a.get("Price")), _exit_board_float(a.get("Qty"))
        if bp > 0 and bq > 0:
            bids.append({"level": i, "price": bp, "qty": bq})
        if ap > 0 and aq > 0:
            asks.append({"level": i, "price": ap, "qty": aq})

    bid = _exit_board_float(raw.get("BidPrice")) or (bids[0]["price"] if bids else 0.0)
    ask = _exit_board_float(raw.get("AskPrice")) or (asks[0]["price"] if asks else 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    board = {"bid": bid, "ask": ask, "bids": bids, "asks": asks, "source": "rest_board"}
    _EXIT_BOARD_CACHE[symbol] = (now, board)
    return dict(board)


def _exit_board_tick_size(price: float) -> float:
    try:
        from utils_common import get_tick_size
        t = float(get_tick_size(price))
        return t if t > 0 else 1.0
    except Exception:
        return 1.0


def _exit_board_round_price(price: float, close_side: str) -> float:
    t = _exit_board_tick_size(price)
    if str(close_side or "").upper() == "BUY":
        return math.ceil(price / t) * t
    return math.floor(price / t) * t


def _exit_board_thick(levels: list[dict], fallback_price: float) -> dict:
    min_qty = _exit_board_env_float("EXIT_REST_FULL_BOARD_THICK_MIN_QTY", 500.0)
    usable = [x for x in levels if _exit_board_float(x.get("qty")) >= min_qty]
    src = usable or levels or [{"level": 1, "price": fallback_price, "qty": 0.0}]
    return max(src, key=lambda x: _exit_board_float(x.get("qty")))


def _exit_board_sum_qty(levels: list[dict], n: int) -> float:
    return sum(_exit_board_float(x.get("qty")) for x in levels[: max(1, n)])


def _exit_board_rest_limit_price(symbol: str, close_side: str, exchange: int):
    board = _exit_board_fetch_rest(str(symbol), exchange)
    if not board:
        return None

    bid, ask = _exit_board_float(board.get("bid")), _exit_board_float(board.get("ask"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    max_spread = _exit_board_env_float(
        "EXIT_REST_FULL_BOARD_MAX_SPREAD_PCT", _exit_board_env_float("ENTRY_MAX_SPREAD_PCT", 0.15)
    )
    if spread_pct > max_spread and _exit_board_env_bool("EXIT_REST_FULL_BOARD_STRICT_SPREAD", False):
        logger.warning(
            "[EXIT REST FULL BOARD] spread too wide -> fallback symbol=%s close_side=%s bid=%s ask=%s spread=%.4f max=%.4f",
            symbol, close_side, bid, ask, spread_pct, max_spread,
        )
        return None

    side = str(close_side or "").upper()
    t = _exit_board_tick_size(mid)
    bids, asks = list(board.get("bids") or []), list(board.get("asks") or [])
    n = int(_exit_board_env_float("EXIT_REST_FULL_BOARD_IMBALANCE_DEPTH", 5))

    if side == "SELL":
        # BUY建玉の返済売り。厚い買い板の価格を参考にしつつ、原則bidタッチで逃げる。
        lv = _exit_board_thick(bids, bid)
        support = _exit_board_float(lv.get("price"), bid)
        price = max(min(bid, support), support)
    elif side == "BUY":
        # SELL建玉の返済買い。厚い売り板の価格を参考にしつつ、原則askタッチで逃げる。
        lv = _exit_board_thick(asks, ask)
        resistance = _exit_board_float(lv.get("price"), ask)
        price = min(max(ask, resistance), resistance)
    else:
        return None

    mode = "EXIT_REST_SELL_CLOSE_THICK_BID" if side == "SELL" else "EXIT_REST_BUY_CLOSE_THICK_ASK"

    # 急ぐ返済なので、厚い板が遠すぎても現在のbest bid/askから離しすぎない。
    max_ticks_away = max(0, int(_exit_board_env_float("EXIT_REST_FULL_BOARD_MAX_TICKS_AWAY", 0)))
    if side == "SELL":
        price = max(price, bid - t * max_ticks_away)
    else:
        price = min(price, ask + t * max_ticks_away)

    meta = {
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "tick": t,
        "thick_level": lv,
        "bid_total_topN": _exit_board_sum_qty(bids, n),
        "ask_total_topN": _exit_board_sum_qty(asks, n),
        "cache_hit": bool(board.get("cache_hit")),
    }
    return _exit_board_round_price(price, side), mode, meta


def _apply_exit_limit_board_touch(payload: dict[str, Any], *, symbol: str, close_side: str, qty: float, exchange: int) -> dict[str, Any]:
    """成行の返済payloadを板タッチ指値へ書き換える。板が取れなければ payload をそのまま返す。"""
    if not _exit_board_env_bool("EXIT_LIMIT_BOARD_TOUCH_ENABLED", True):
        return payload

    close_side_u = str(close_side or "").upper()
    limit_price = 0.0
    rule = ""
    rest_meta = None

    rest_decision = _exit_board_rest_limit_price(str(symbol), close_side_u, int(exchange or 1))
    if rest_decision is not None:
        limit_price, rule, rest_meta = rest_decision
    else:
        bid, ask = _exit_board_get_bid_ask(str(symbol))
        if close_side_u == "SELL" and bid > 0:
            limit_price = bid
            rule = "SELL_CLOSE_AT_BID_PUSH_FALLBACK"
        elif close_side_u == "BUY" and ask > 0:
            limit_price = ask
            rule = "BUY_CLOSE_AT_ASK_PUSH_FALLBACK"

    if limit_price > 0:
        payload["FrontOrderType"] = 20
        payload["Price"] = float(limit_price)
        try:
            payload["_rest_full_board_exit"] = bool(rest_meta)
            if rest_meta:
                payload["_rest_full_board_exit_meta"] = rest_meta
        except Exception:
            pass
        logger.warning(
            "[EXIT LIMIT BOARD TOUCH] market->limit symbol=%s close_side=%s rule=%s price=%s qty=%s rest_meta=%s",
            symbol, close_side_u, rule, limit_price, qty, rest_meta,
        )
        return payload

    if _exit_board_env_bool("EXIT_LIMIT_FALLBACK_MARKET_IF_NO_BOARD", True):
        logger.warning(
            "[EXIT LIMIT BOARD TOUCH] board missing -> fallback MARKET symbol=%s close_side=%s",
            symbol, close_side_u,
        )
        return payload

    logger.warning(
        "[EXIT LIMIT BOARD TOUCH] board missing and market fallback disabled symbol=%s close_side=%s",
        symbol, close_side_u,
    )
    return payload


def _build_kabu_close_payload(*, symbol: str, close_side: str, qty: float, exchange: int = 1) -> dict[str, Any]:
    side_num = 1 if close_side == "SELL" else 2
    payload = {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": int(exchange),
        "SecurityType": 1,
        "Side": int(side_num),
        "CashMargin": 3,
        "MarginTradeType": 1,
        "DelivType": 2,
        "AccountType": 4,
        "Qty": int(qty),
        "ClosePositionOrder": 4,
        "FrontOrderType": 10,
        "Price": 0,
        "ExpireDay": 0,
    }

    # 旧 exit_limit_board_touch_runtime_patch から移設。
    if _exit_board_runtime_enabled():
        payload = _apply_exit_limit_board_touch(
            payload, symbol=symbol, close_side=close_side, qty=qty, exchange=exchange
        )
    return payload


# board_runtime_self_check_patch 互換マーカー。
# 板タッチ指値は本文化済みなので、self check には常に「有効」と見せる。
_build_kabu_close_payload._exit_limit_board_touch_builtin = True  # type: ignore[attr-defined]
_build_kabu_close_payload._exit_limit_board_touch_wrapped_v11 = True  # type: ignore[attr-defined]


def _send_kabu_close_order_builtin(*, symbol: str, side: str, close_side: str, qty: float, exit_price: float, reason: str, position: dict[str, Any]) -> tuple[bool, str, str]:
    try:
        exchange = _safe_int(position.get("exchange") or position.get("Exchange"), 1)
        payload = _build_kabu_close_payload(symbol=symbol, close_side=close_side, qty=qty, exchange=exchange)
        logger.warning(
            "[EXIT_EXECUTOR] builtin kabu close order send symbol=%s side=%s close_side=%s qty=%s ref_price=%.4f reason=%s payload=%s",
            symbol,
            side,
            close_side,
            int(qty),
            exit_price,
            reason,
            payload,
        )
        ret = send_order_common(payload)
        ok, order_id, message = _parse_order_result(ret)
        if ok:
            logger.info("[EXIT_EXECUTOR] builtin close order accepted symbol=%s order_id=%s message=%s", symbol, order_id, message)
        else:
            logger.error("[EXIT_EXECUTOR] builtin close order rejected symbol=%s message=%s", symbol, message)
        return ok, order_id, message
    except Exception as e:
        logger.exception("[EXIT_EXECUTOR] builtin close order failed symbol=%s", symbol)
        return False, "", str(e)


def _send_close_order(*, symbol: str, side: str, close_side: str, qty: float, exit_price: float, reason: str, position: dict[str, Any]) -> tuple[bool, str, str]:
    if EXIT_DRY_RUN_ORDER_SUCCESS:
        logger.warning(
            "[EXIT_EXECUTOR] DRY_RUN order success enabled. No real close order sent. symbol=%s close_side=%s qty=%.4f price=%.4f reason=%s",
            symbol,
            close_side,
            qty,
            exit_price,
            reason,
        )
        return True, "DRY_RUN", "dry run order success"

    sender = _get_injected_sender()
    if callable(sender):
        try:
            ret = sender(symbol=symbol, close_side=close_side, qty=qty, price=exit_price, reason=reason, position=position)
            ok, order_id, message = _parse_order_result(ret)
            if ok:
                logger.info(
                    "[EXIT_EXECUTOR] injected close order accepted symbol=%s close_side=%s qty=%.4f price=%.4f order_id=%s message=%s",
                    symbol,
                    close_side,
                    qty,
                    exit_price,
                    order_id,
                    message,
                )
            else:
                logger.error(
                    "[EXIT_EXECUTOR] injected close order rejected symbol=%s close_side=%s qty=%.4f price=%.4f message=%s",
                    symbol,
                    close_side,
                    qty,
                    exit_price,
                    message,
                )
            return ok, order_id, message
        except Exception as e:
            logger.exception("[EXIT_EXECUTOR] injected close order sender failed symbol=%s close_side=%s qty=%.4f price=%.4f", symbol, close_side, qty, exit_price)
            return False, "", str(e)

    logger.warning("[EXIT_EXECUTOR] injected close order sender not connected -> use builtin kabu close order symbol=%s", symbol)
    return _send_kabu_close_order_builtin(symbol=symbol, side=side, close_side=close_side, qty=qty, exit_price=exit_price, reason=reason, position=position)


def _calculate_pnl(*, side: str, qty: float, avg_price: float, exit_price: float) -> float:
    side = _normalize_side(side)
    if side == "BUY":
        pnl = (exit_price - avg_price) * qty
    elif side == "SELL":
        pnl = (avg_price - exit_price) * qty
    else:
        pnl = 0.0
    return _safe_float(pnl)


def _restore_open_status(symbol: str, pos: dict[str, Any]) -> None:
    try:
        if pos.get("status") == "CLOSING":
            pos["status"] = "OPEN"
            pos["updated_at"] = dt.datetime.now()
    except Exception:
        logger.warning("[EXIT_EXECUTOR] failed to restore OPEN status symbol=%s", symbol, exc_info=True)


def _close_db_position(*, symbol: str, exit_price: float, pnl: float, reason: str, order_id: str) -> bool:
    session = Session_position()
    try:
        db_pos = (
            session.query(Position)
            .filter(Position.symbol == symbol)
            .filter(Position.status == "OPEN")
            .first()
        )
        if not db_pos:
            logger.warning("[EXIT_EXECUTOR] DB open position not found symbol=%s", symbol)
            session.rollback()
            return False
        now = dt.datetime.now()
        db_pos.exit_price = exit_price
        db_pos.status = "CLOSED"
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
        logger.info("[EXIT_EXECUTOR] DB position closed symbol=%s exit_price=%.4f pnl=%.4f order_id=%s", symbol, exit_price, pnl, order_id)
        return True
    except Exception:
        session.rollback()
        logger.exception("[EXIT_EXECUTOR_DB_ERROR] symbol=%s", symbol)
        return False
    finally:
        session.close()


def _remove_from_memory_positions(symbol: str) -> None:
    try:
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict) and symbol in positions:
            del positions[symbol]
    except Exception:
        logger.warning("[EXIT_EXECUTOR] failed to remove from global_data.open_positions symbol=%s", symbol, exc_info=True)
    try:
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is None:
            return
        for method_name in ["remove", "close", "mark_closed", "delete"]:
            fn = getattr(positions_obj, method_name, None)
            if callable(fn):
                try:
                    fn(symbol)
                    return
                except Exception:
                    pass
        raw = getattr(positions_obj, "open_positions", None)
        if isinstance(raw, dict) and symbol in raw:
            del raw[symbol]
    except Exception:
        logger.debug("[EXIT_EXECUTOR] failed to remove from GC.positions symbol=%s", symbol, exc_info=True)


def execute_exit(symbol: str, reason: str, exit_price: float) -> bool:
    symbol = _normalize_symbol(symbol)
    if not symbol:
        logger.warning("[EXIT_EXECUTOR] empty symbol")
        return False

    with _exit_lock:
        pos: Optional[dict[str, Any]] = None
        try:
            pos, pos_source = _get_open_position(symbol)
            if pos is None:
                logger.warning("[EXIT_EXECUTOR] no open position symbol=%s", symbol)
                return False

            status = str(pos.get("status") or "OPEN").upper()
            if status in {"CLOSING", "CLOSED"}:
                logger.debug("[EXIT_EXECUTOR] already closing/closed symbol=%s status=%s", symbol, status)
                return False

            pos["status"] = "CLOSING"
            pos["updated_at"] = dt.datetime.now()

            side = _normalize_side(pos.get("side"))
            close_side = _close_side_from_position_side(side)
            qty = _safe_float(pos.get("qty") or pos.get("quantity"))
            avg_price = _safe_float(pos.get("avg_price") or pos.get("entry_price"))
            exit_price = _safe_float(exit_price)

            if side not in {"BUY", "SELL"}:
                logger.warning("[EXIT_EXECUTOR] invalid side symbol=%s side=%s pos=%s", symbol, side, pos)
                _restore_open_status(symbol, pos)
                return False
            if close_side not in {"BUY", "SELL"}:
                logger.warning("[EXIT_EXECUTOR] invalid close_side symbol=%s side=%s close_side=%s", symbol, side, close_side)
                _restore_open_status(symbol, pos)
                return False
            if qty <= 0 or avg_price <= 0 or exit_price <= 0:
                logger.warning("[EXIT_EXECUTOR] invalid numeric values symbol=%s qty=%.4f avg=%.4f exit=%.4f pos=%s", symbol, qty, avg_price, exit_price, pos)
                _restore_open_status(symbol, pos)
                return False

            logger.warning(
                "🚪 EXIT REQUEST symbol=%s side=%s close_side=%s qty=%.4f price=%.4f reason=%s pos_source=%s",
                symbol,
                side,
                close_side,
                qty,
                exit_price,
                reason,
                pos_source,
            )

            order_ok, order_id, order_message = _send_close_order(symbol=symbol, side=side, close_side=close_side, qty=qty, exit_price=exit_price, reason=reason, position=pos)

            if EXIT_REQUIRE_ORDER_SUCCESS and not order_ok:
                logger.error("[EXIT_EXECUTOR] exit aborted because close order failed. symbol=%s message=%s", symbol, order_message)
                _restore_open_status(symbol, pos)
                return False

            pnl = _calculate_pnl(side=side, qty=qty, avg_price=avg_price, exit_price=exit_price)
            db_ok = _close_db_position(symbol=symbol, exit_price=exit_price, pnl=pnl, reason=reason, order_id=order_id)
            if not db_ok:
                logger.error("[EXIT_EXECUTOR] close order may be accepted but DB close failed. symbol=%s order_id=%s", symbol, order_id)
                pos["status"] = "CLOSING"
                pos["exit_order_id"] = order_id
                pos["exit_order_message"] = order_message
                pos["updated_at"] = dt.datetime.now()
                return False

            pos["exit_price"] = exit_price
            pos["pnl"] = pnl
            pos["status"] = "CLOSED"
            pos["exit_time"] = dt.datetime.now()
            pos["exit_reason"] = reason
            pos["exit_order_id"] = order_id
            pos["exit_order_message"] = order_message
            pos["updated_at"] = dt.datetime.now()
            logger.warning("✅ EXIT COMPLETE symbol=%s pnl=%.4f reason=%s order_id=%s", symbol, pnl, reason, order_id)
            _remove_from_memory_positions(symbol)
            return True
        except Exception:
            logger.exception("[EXIT_EXECUTOR_FATAL] symbol=%s", symbol)
            if pos is not None:
                _restore_open_status(symbol, pos)
            return False


__all__ = ["set_exit_order_sender", "execute_exit"]
