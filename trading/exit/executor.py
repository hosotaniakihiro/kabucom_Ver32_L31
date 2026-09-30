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
from typing import Any, Callable, Optional

from core.global_context.context import global_context as GC
from database import Session_position
from database.models import Position
from global_state import global_data
from kabu_api import kabu_http
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
        # 2026-09-18: status は完全一致だけでは足りない。positions.db には
        #   "CLOSED_BROKER_NOT_OPEN" のような接尾辞付きが入っていて、これが
        #   「未決済」として拾われ、9/14 の決済済み建玉 ID で決済しようとして
        #   kabu API に Code=8「決済指定内容に誤りがあります」で弾かれていた。
        closed_prefix = ("CLOSED", "CLOSE", "EXIT", "CANCEL", "REJECT", "FAIL")
        candidates: list[tuple[str, dict[str, Any]]] = []
        best: Optional[dict[str, Any]] = None
        for p in rows or []:
            status = str(getattr(p, "status", "") or "OPEN").strip().upper()
            if status in closed or status.startswith(closed_prefix):
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
                # 2026-09-18: 最初に見つかった 1 件で break すると id の小さい
                #   (= 古い) 玉を掴む。entry_time の新しい順で選び直す。
                candidates.append((str(getattr(p, "entry_time", "") or ""), d))
        if candidates:
            candidates.sort(key=lambda t: t[0], reverse=True)
            best = candidates[0][1]
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


def _get_open_position_core(symbol: str) -> tuple[Optional[dict[str, Any]], str]:
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
    _EXIT_BOARD_LAST_CALL[symbol] = now
    try:
        # urllib.request の "Connection: close" では kabu Station が応答を
        # 返さない (詳細は kabu_api/kabu_http.py)。板読みも requests 経由にする。
        raw = kabu_http.request_json(
            url,
            method="GET",
            api_key=token,
            timeout=_exit_board_env_float("EXIT_REST_FULL_BOARD_TIMEOUT_SEC", 0.8),
        )
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


def _exec_price_from_order_message(message: Any) -> float:
    """返済結果 message ({'order_id': ..., 'exec_price': 3245.0, ...} の文字列) から約定価格。無ければ 0。"""
    try:
        if isinstance(message, dict):
            return _safe_float(message.get("exec_price"), 0.0)
        import re as _re

        m = _re.search(r"'exec_price':\s*([0-9.]+)", str(message or ""))
        return float(m.group(1)) if m else 0.0
    except Exception:
        return 0.0


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


# 旧 core/startup/exit_limit_pending_close_runtime_patch.py 由来。
# 指値EXITは注文受付だけではまだ未約定のことがあるため、原則DBはCLOSEDに
# せずCLOSINGへ据え置く (実約定確認は _exit_fill_confirm_loop / 失敗時の
# 救済は _exit_closing_reconcile_loop に任せる)。
EXIT_LIMIT_PENDING_CLOSE_ENABLED = _exit_board_env_bool("EXIT_LIMIT_PENDING_CLOSE_ENABLED", True)
EXIT_MARK_CLOSED_ON_ORDER_ACCEPT = _exit_board_env_bool("EXIT_MARK_CLOSED_ON_ORDER_ACCEPT", False)


# 2026-09-29: positions テーブルには order_id / exit_order_id / pnl 列が無い
# (database.models.Position)。下の setattr は hasattr で黙って飛ばされ、
# 返済注文番号は一度も DB に残っていなかった。そのため約定確認は毎回
# 「同じ銘柄の返済注文の先頭」に当たり、別の注文の約定で行を閉じていた。
# 行 id -> 返済注文番号をプロセス内で持つ (再起動後は当日分の fallback)。
_EXIT_ORDER_ID_BY_ROW: dict[int, str] = {}


def _mark_db_position_closing(*, symbol: str, exit_price: float, pnl: float, reason: str, order_id: str) -> bool:
    """旧 exit_limit_pending_close_runtime_patch._mark_db_closing 相当。

    返済注文受付直後はまだ未約定のことがあるため、DBはCLOSEDにせず
    CLOSINGへ据え置く。OPEN/CLOSING いずれの行も対象にする
    (再送・多重呼び出しで既にCLOSING化済みの行も更新できるようにするため)。
    """
    session = Session_position()
    try:
        db_pos = (
            session.query(Position)
            .filter(Position.symbol == str(symbol))
            .filter(Position.status.in_(["OPEN", "CLOSING"]))
            .first()
        )
        if not db_pos:
            logger.warning("[EXIT LIMIT PENDING CLOSE] DB open/closing position not found symbol=%s", symbol)
            session.rollback()
            return False
        now = dt.datetime.now()
        db_pos.status = "CLOSING"
        for attr, value in [
            ("exit_price", _safe_float(exit_price)),
            ("updated_at", now),
            ("pnl", _safe_float(pnl)),
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
        if order_id and getattr(db_pos, "id", None) is not None:
            _EXIT_ORDER_ID_BY_ROW[int(db_pos.id)] = str(order_id)
        logger.warning(
            "[EXIT LIMIT PENDING CLOSE] DB marked CLOSING symbol=%s exit_price=%.4f pnl=%.4f order_id=%s reason=%s",
            symbol, _safe_float(exit_price), _safe_float(pnl), order_id, reason,
        )
        return True
    except Exception:
        session.rollback()
        logger.exception("[EXIT LIMIT PENDING CLOSE] DB mark closing failed symbol=%s", symbol)
        return False
    finally:
        session.close()


def _close_db_position_core(*, symbol: str, exit_price: float, pnl: float, reason: str, order_id: str) -> bool:
    # 板タッチ指値EXIT運用では、注文受付直後にDBをCLOSED確定すると未約定の
    # まま建玉が閉じた扱いになるため、既定ではCLOSINGへ据え置く
    # (旧 exit_limit_pending_close_runtime_patch)。成行運用へ戻したい場合は
    # EXIT_MARK_CLOSED_ON_ORDER_ACCEPT=1 で従来通りの即時CLOSEDに戻せる。
    if EXIT_LIMIT_PENDING_CLOSE_ENABLED and not EXIT_MARK_CLOSED_ON_ORDER_ACCEPT:
        return _mark_db_position_closing(symbol=symbol, exit_price=exit_price, pnl=pnl, reason=reason, order_id=order_id)

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

            # 2026-09-30: 約定が取れていれば判定価格ではなく約定価格で損益を出す。
            #   3110 は再発注価格 3255 で +1,350 円と記録され、実約定は 3245 の -650 円だった。
            _exec_px = _exec_price_from_order_message(order_message)
            if _exec_px > 0 and abs(_exec_px - exit_price) > 1e-9:
                logger.warning(
                    "[EXIT_EXECUTOR] pnl uses executed price symbol=%s decided=%.4f executed=%.4f",
                    symbol, exit_price, _exec_px,
                )
                exit_price = _exec_px
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


# ============================================================
# 旧 core/startup/exit_order_fill_confirm_runtime_patch.py 由来。
#
# 指値EXITを注文受付時点でCLOSINGにした後、kabusapi/orders を監視して
# 全約定ならpositions.dbを正式CLOSEDに確定するバックグラウンドループ。
#
# 目的:
#   - 未約定のままCLOSEDにしない
#   - 約定済みなのにCLOSINGで残さない
#   - 部分約定はCLOSING維持
# ============================================================

EXIT_FILL_CONFIRM_ENABLED = _exit_board_env_bool("EXIT_FILL_CONFIRM_ENABLED", True)

_exit_fill_confirm_installed = False
_exit_fill_confirm_thread_started = False


def _exit_fill_confirm_order_id(v: Any) -> str:
    return str(v or "").strip()


def _exit_fill_confirm_remaining(order: dict[str, Any]) -> tuple[float, float, float]:
    qty = _safe_float(order.get("OrderQty", order.get("Qty", 0)), 0.0)
    cum = _safe_float(order.get("CumQty", order.get("ContractedQty", 0)), 0.0)
    leaves = _safe_float(order.get("LeavesQty", 0), 0.0)
    if leaves <= 0 and qty > 0:
        leaves = max(qty - cum, 0.0)
    if qty <= 0 and leaves > 0:
        qty = leaves + cum
    return qty, cum, leaves


def _exit_fill_confirm_is_filled(order: dict[str, Any]) -> bool:
    state = str(order.get("State") or "").strip()
    qty, cum, leaves = _exit_fill_confirm_remaining(order)
    # kabuS APIでは状態値の詳細が環境差で揺れるため、数量を主判定にする。
    if qty > 0 and cum >= qty and leaves <= 0:
        return True
    if leaves <= 0 and cum > 0 and state not in {"1", "2", "3", "4"}:
        return True
    return False


def _exit_fill_confirm_find_order(orders: list[dict], wanted_order_id: str, symbol: str) -> Optional[dict]:
    wanted_order_id = _exit_fill_confirm_order_id(wanted_order_id)
    symbol = _normalize_symbol(symbol)
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        oid = _exit_fill_confirm_order_id(o.get("OrderId") or o.get("ID"))
        if wanted_order_id and oid == wanted_order_id:
            return o
    # order_idが空/欠落した古いDB行向けの補助。返済注文だけを拾う。
    # 2026-09-29: order_id を持つ行はここへ来させない。/orders は前日以前の
    # 注文も返すので、6904 の行が 9/25 の返済注文で CLOSED 確定されていた。
    # 当日受付の注文に限る。
    if wanted_order_id:
        return None
    today = dt.date.today().isoformat()
    best = None
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        if _normalize_symbol(o.get("Symbol")) != symbol:
            continue
        recv = str(o.get("RecvTime") or "")
        if not recv.startswith(today):
            continue
        try:
            if int(float(o.get("CashMargin") or 0)) != 3:
                continue
        except Exception:
            continue
        # 同じ銘柄を何度も返済する日があるので最新の返済注文を使う
        if best is None or recv > str(best.get("RecvTime") or ""):
            best = o
    return best


def _exit_fill_confirm_exec_price(order: dict) -> float:
    """約定明細 (RecType=8) の数量加重平均。成行の order["Price"] は 0。"""
    try:
        from kabu_api.close import summarize_executions

        ex = summarize_executions(order)
        if ex:
            return _safe_float(ex.get("price"), 0.0)
    except Exception:
        logger.debug("[EXIT FILL CONFIRM] summarize_executions failed", exc_info=True)
    return _safe_float(order.get("Price"), 0.0)


def _exit_fill_confirm_close_db_and_memory(symbol: str, order_id: str, order: dict, row_id: Any = None) -> bool:
    symbol = _normalize_symbol(symbol)
    session = None
    try:
        session = Session_position()
        # 2026-09-29: 同じ銘柄の CLOSING 行が複数あると .first() が別の行を
        # 閉じていた (5301 の 13:48 の約定で 12:40 の行が +1,820 円で確定)。
        # 約定確認した行そのものを閉じる。
        q = session.query(Position).filter(Position.status == "CLOSING")
        if row_id is not None:
            q = q.filter(Position.id == row_id)
        else:
            q = q.filter(Position.symbol == symbol)
        row = q.first()
        if not row:
            session.rollback()
            return False
        now = dt.datetime.now()
        avg_price = _safe_float(getattr(row, "avg_price", 0) or getattr(row, "entry_price", 0) or getattr(row, "price", 0), 0.0)
        qty = _safe_float(getattr(row, "qty", 0), 0.0)
        exit_price = _exit_fill_confirm_exec_price(order) or _safe_float(getattr(row, "exit_price", 0), 0.0)
        side = str(getattr(row, "side", "") or "").upper()
        pnl = 0.0
        if qty > 0 and avg_price > 0 and exit_price > 0:
            # side は "BUY_CREDIT" / "SELL_CREDIT" でも入る
            pnl = (exit_price - avg_price) * qty if side.startswith("BUY") else (avg_price - exit_price) * qty
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
        _EXIT_ORDER_ID_BY_ROW.pop(int(getattr(row, "id", 0) or 0), None)
        try:
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


def _exit_fill_confirm_closing_rows() -> list[dict]:
    out: list[dict] = []
    session = None
    try:
        session = Session_position()
        rows = session.query(Position).filter(Position.status == "CLOSING").all()
        for r in rows or []:
            sym = _normalize_symbol(getattr(r, "symbol", ""))
            oid = _exit_fill_confirm_order_id(
                getattr(r, "exit_order_id", "")
                or getattr(r, "order_id", "")
                or _EXIT_ORDER_ID_BY_ROW.get(int(getattr(r, "id", 0) or 0), "")
            )
            if sym:
                out.append({"symbol": sym, "order_id": oid, "row_id": getattr(r, "id", None)})
    except Exception:
        logger.debug("[EXIT FILL CONFIRM] load CLOSING rows failed", exc_info=True)
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass
    return out


def _exit_fill_confirm_loop() -> None:
    try:
        import force_cancel_loop as fcl
    except Exception:
        logger.exception("[EXIT FILL CONFIRM] import force_cancel_loop failed")
        return
    get_orders = getattr(fcl, "get_orders", None)
    if not callable(get_orders):
        logger.warning("[EXIT FILL CONFIRM] get_orders missing")
        return
    interval = max(0.5, _exit_board_env_float("EXIT_FILL_CONFIRM_INTERVAL_SEC", 1.0))
    logger.warning("[EXIT FILL CONFIRM] loop start interval=%.2fs", interval)
    while True:
        try:
            rows = _exit_fill_confirm_closing_rows()
            if not rows:
                time.sleep(interval)
                continue
            orders = get_orders() or []
            for row in rows:
                sym = row.get("symbol") or ""
                oid = row.get("order_id") or ""
                order = _exit_fill_confirm_find_order(orders, oid, sym)
                if order and _exit_fill_confirm_is_filled(order):
                    _exit_fill_confirm_close_db_and_memory(
                        sym,
                        oid or _exit_fill_confirm_order_id(order.get("OrderId") or order.get("ID")),
                        order,
                        row_id=row.get("row_id"),
                    )
                elif order:
                    qty, cum, leaves = _exit_fill_confirm_remaining(order)
                    logger.info("[EXIT FILL CONFIRM] still closing symbol=%s order_id=%s cum=%.0f leaves=%.0f qty=%.0f", sym, oid, cum, leaves, qty)
                else:
                    logger.debug("[EXIT FILL CONFIRM] order not found yet symbol=%s order_id=%s", sym, oid)
        except Exception:
            logger.exception("[EXIT FILL CONFIRM] loop error")
        time.sleep(interval)


def start_exit_fill_confirm_loop() -> bool:
    """CLOSING建玉の約定確認バックグラウンドループを起動する (冪等)。"""
    global _exit_fill_confirm_installed, _exit_fill_confirm_thread_started
    if _exit_fill_confirm_installed:
        return True
    if not EXIT_FILL_CONFIRM_ENABLED:
        logger.warning("[EXIT FILL CONFIRM] disabled")
        return False
    if not _exit_fill_confirm_thread_started:
        threading.Thread(target=_exit_fill_confirm_loop, daemon=True, name="exit_fill_confirm_loop").start()
        _exit_fill_confirm_thread_started = True
    _exit_fill_confirm_installed = True
    logger.warning("[EXIT FILL CONFIRM] installed interval=%.2f", _exit_board_env_float("EXIT_FILL_CONFIRM_INTERVAL_SEC", 1.0))
    return True


# ============================================================
# 旧 core/startup/exit_closing_stale_reconcile_runtime_patch.py 由来。
#
# CLOSINGのまま長時間残った建玉を救済するバックグラウンドループ。
#
# 方針:
#   - positions.db の CLOSING が一定秒数以上残ったら確認対象
#   - 原則、ブローカーREST建玉取得に成功した時だけOPEN/CLOSEDへ救済
#   - ブローカー建玉に同銘柄が残っていなければ CLOSED 確定
#   - ブローカー建玉に同銘柄が残っていれば OPEN に戻す
#   - ブローカー取得に失敗した場合は何もしない
#   - 現物保有銘柄 (既定 147A) は対象から除外する
# ============================================================

EXIT_CLOSING_RECONCILE_ENABLED = _exit_board_env_bool("EXIT_CLOSING_RECONCILE_ENABLED", True)
_EXIT_CLOSING_RECONCILE_DEFAULT_IGNORE_SYMBOLS = "147A"

_exit_closing_reconcile_installed = False
_exit_closing_reconcile_thread_started = False


def _exit_closing_reconcile_ignored_symbols() -> set[str]:
    raw = os.getenv("EXIT_CLOSING_RECONCILE_IGNORE_SYMBOLS", _EXIT_CLOSING_RECONCILE_DEFAULT_IGNORE_SYMBOLS)
    out: set[str] = set()
    for part in str(raw or "").replace(";", ",").split(","):
        sym = _normalize_symbol(part)
        if sym:
            out.add(sym)
    return out


def _exit_closing_reconcile_is_ignored_symbol(sym: str) -> bool:
    return bool(sym) and sym in _exit_closing_reconcile_ignored_symbols()


def _exit_closing_reconcile_int_or_none(v: Any) -> Optional[int]:
    try:
        if v in (None, ""):
            return None
        return int(float(str(v).strip()))
    except Exception:
        return None


def _exit_closing_reconcile_is_cash_position(p: dict) -> bool:
    """kabuS /positions の現物を検出する。

    MarginTradeType=0 は現物。Product/ProductType=0/1 も現物側として扱う。
    フィールドが無い場合は信用か不明なので除外しない。
    """
    margin_trade_type = _exit_closing_reconcile_int_or_none(
        p.get("MarginTradeType")
        or p.get("margin_trade_type")
        or p.get("MarginType")
        or p.get("marginType")
    )
    if margin_trade_type == 0:
        return True

    product = _exit_closing_reconcile_int_or_none(
        p.get("Product")
        or p.get("product")
        or p.get("ProductType")
        or p.get("product_type")
    )
    if product in {0, 1}:
        return True

    return False


def _exit_closing_reconcile_row_age_sec(row: Any) -> float:
    now = dt.datetime.now()
    for attr in ("updated_at", "exit_time", "created_at", "entry_time"):
        try:
            raw = getattr(row, attr, None)
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            if isinstance(raw, dt.datetime):
                return max(0.0, (now - raw.replace(tzinfo=None)).total_seconds())
            s = str(raw).strip().replace("T", " ")
            if "+" in s:
                s = s.split("+", 1)[0]
            if s.endswith("Z"):
                s = s[:-1]
            return max(0.0, (now - dt.datetime.fromisoformat(s)).total_seconds())
        except Exception:
            pass
    return 999999.0


def _exit_closing_reconcile_extract_position_rows(ret: Any) -> list[dict]:
    if isinstance(ret, dict):
        rows = ret.get("Positions") or ret.get("positions") or ret.get("Data") or ret.get("data") or []
    else:
        rows = ret
    if isinstance(rows, list):
        return [x for x in rows if isinstance(x, dict)]
    return []


def _exit_closing_reconcile_rows_to_open_symbols(rows: list[dict]) -> set[str]:
    out: set[str] = set()
    ignored = _exit_closing_reconcile_ignored_symbols()
    skipped_cash = 0
    skipped_ignored = 0
    for p in rows or []:
        sym = _normalize_symbol(p.get("Symbol") or p.get("symbol") or p.get("Code") or p.get("code"))
        if not sym:
            continue
        if sym in ignored:
            skipped_ignored += 1
            continue
        if _exit_closing_reconcile_is_cash_position(p):
            skipped_cash += 1
            continue
        qty = _safe_float(
            p.get("LeavesQty")
            or p.get("HoldQty")
            or p.get("Qty")
            or p.get("qty")
            or p.get("LeavesQuantity")
            or p.get("HoldQuantity"),
            0.0,
        )
        if sym and qty > 0:
            out.add(sym)
    if skipped_cash or skipped_ignored:
        logger.debug(
            "[EXIT CLOSING RECONCILE] filtered broker positions cash=%s ignored=%s ignore_symbols=%s",
            skipped_cash,
            skipped_ignored,
            sorted(ignored),
        )
    return out


def _exit_closing_reconcile_get_token() -> Optional[str]:
    try:
        from token_manager import get_valid_token, refresh_token
        return get_valid_token() or refresh_token()
    except Exception:
        logger.debug("[EXIT CLOSING RECONCILE] token unavailable", exc_info=True)
        return None


def _exit_closing_reconcile_fetch_positions_rest_direct() -> tuple[bool, set[str]]:
    token = _exit_closing_reconcile_get_token()
    if not token:
        return False, set()
    base = os.getenv("KABUSAPI_BASE_URL", "http://localhost:18080/kabusapi").rstrip("/")
    # 信用建玉だけを判定対象にしたいので product=2 を先に試す。
    # all/product=0 は環境差吸収用の補助。ただし _rows_to_open_symbols で現物は除外する。
    urls = [
        f"{base}/positions?{urllib.parse.urlencode({'product': 2})}",
        f"{base}/positions",
        f"{base}/positions?{urllib.parse.urlencode({'product': 0})}",
    ]
    for url in urls:
        try:
            # urllib.request の "Connection: close" では kabu Station が応答を
            # 返さない (詳細は kabu_api/kabu_http.py)。
            raw = kabu_http.request_json(
                url,
                method="GET",
                api_key=token,
                timeout=_exit_board_env_float("EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC", 1.5),
            )
            rows = _exit_closing_reconcile_extract_position_rows(raw)
            symbols = _exit_closing_reconcile_rows_to_open_symbols(rows)
            logger.info("[EXIT CLOSING RECONCILE] REST positions ok url=%s rows=%s credit_symbols=%s ignored=%s", url, len(rows), sorted(symbols)[:20], sorted(_exit_closing_reconcile_ignored_symbols()))
            return True, symbols
        except Exception:
            logger.debug("[EXIT CLOSING RECONCILE] direct REST positions failed url=%s", url, exc_info=True)
            continue
    return False, set()


def _exit_closing_reconcile_load_broker_open_symbols() -> tuple[bool, set[str]]:
    # 1. まずブローカーREST helperを試す。
    candidates = [
        ("kabu_api.positions", "get_positions"),
        ("kabu_api.position", "get_positions"),
        ("kabu_api.get_positions", "get_positions"),
        ("kabu_api.api_positions", "get_positions"),
    ]
    for mod_name, fn_name in candidates:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)
            if not callable(fn):
                continue
            ret = fn()
            rows = _exit_closing_reconcile_extract_position_rows(ret)
            symbols = _exit_closing_reconcile_rows_to_open_symbols(rows)
            logger.info("[EXIT CLOSING RECONCILE] helper positions ok module=%s rows=%s credit_symbols=%s ignored=%s", mod_name, len(rows), sorted(symbols)[:20], sorted(_exit_closing_reconcile_ignored_symbols()))
            return True, symbols
        except Exception:
            continue

    # 2. helperが無い/失敗する場合は直接REST。
    ok, symbols = _exit_closing_reconcile_fetch_positions_rest_direct()
    if ok:
        return True, symbols

    # 3. 最後の補助: 明示許可時だけメモリを見る。デフォルトでは使わない。
    if not _exit_board_env_bool("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK", False):
        return False, set()
    symbols = set()
    ignored = _exit_closing_reconcile_ignored_symbols()
    try:
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            for k, v in positions.items():
                sym = _normalize_symbol(k or (v.get("symbol") if isinstance(v, dict) else ""))
                if not sym or sym in ignored:
                    continue
                if sym:
                    status = str(v.get("status") if isinstance(v, dict) else "OPEN").upper()
                    if status not in {"CLOSED", "CLOSE", "EXITED", "DONE", "CANCELED", "CANCELLED", "REJECTED"}:
                        symbols.add(sym)
            logger.warning("[EXIT CLOSING RECONCILE] memory fallback used credit_symbols=%s ignored=%s", sorted(symbols)[:20], sorted(ignored))
            return True, symbols
    except Exception:
        logger.debug("[EXIT CLOSING RECONCILE] memory fallback failed", exc_info=True)
    return False, set()


def _exit_closing_reconcile_close_row(row: Any, reason: str) -> None:
    now = dt.datetime.now()
    row.status = "CLOSED"
    for attr, value in [
        ("closed_time", now),
        ("close_time", now),
        ("exit_time", now),
        ("updated_at", now),
        ("exit_reason", reason),
        ("close_reason", reason),
    ]:
        try:
            if hasattr(row, attr):
                setattr(row, attr, value)
        except Exception:
            pass


def _exit_closing_reconcile_open_row(row: Any, reason: str) -> None:
    row.status = "OPEN"
    for attr, value in [
        ("updated_at", dt.datetime.now()),
        ("exit_reason", reason),
        ("close_reason", reason),
    ]:
        try:
            if hasattr(row, attr):
                setattr(row, attr, value)
        except Exception:
            pass


def _exit_closing_reconcile_loop() -> None:
    interval = max(1.0, _exit_board_env_float("EXIT_CLOSING_RECONCILE_INTERVAL_SEC", 5.0))
    stale_sec = max(2.0, _exit_board_env_float("EXIT_CLOSING_STALE_SEC", 20.0))
    logger.warning("[EXIT CLOSING RECONCILE] loop start interval=%.1fs stale_sec=%.1fs rest_first=True ignored=%s", interval, stale_sec, sorted(_exit_closing_reconcile_ignored_symbols()))

    while True:
        session = None
        try:
            ok, broker_symbols = _exit_closing_reconcile_load_broker_open_symbols()
            if not ok:
                logger.debug("[EXIT CLOSING RECONCILE] broker REST symbols unavailable -> skip")
                time.sleep(interval)
                continue
            session = Session_position()
            rows = session.query(Position).filter(Position.status == "CLOSING").all()
            changed = 0
            for row in rows or []:
                sym = _normalize_symbol(getattr(row, "symbol", ""))
                if not sym:
                    continue
                if _exit_closing_reconcile_is_ignored_symbol(sym):
                    logger.debug("[EXIT CLOSING RECONCILE] ignored CLOSING symbol=%s", sym)
                    continue
                age = _exit_closing_reconcile_row_age_sec(row)
                if age < stale_sec:
                    continue
                if sym in broker_symbols:
                    _exit_closing_reconcile_open_row(row, "closing_stale_broker_rest_still_open")
                    changed += 1
                    logger.warning("[EXIT CLOSING RECONCILE] stale CLOSING -> OPEN symbol=%s age=%.1fs broker_open=True source=REST", sym, age)
                else:
                    _exit_closing_reconcile_close_row(row, "closing_stale_broker_rest_flat_confirmed")
                    changed += 1
                    logger.warning("[EXIT CLOSING RECONCILE] stale CLOSING -> CLOSED symbol=%s age=%.1fs broker_open=False source=REST", sym, age)
                    try:
                        positions = getattr(global_data, "open_positions", None)
                        if isinstance(positions, dict):
                            positions.pop(sym, None)
                    except Exception:
                        pass
            if changed:
                session.commit()
            else:
                session.rollback()
        except Exception:
            try:
                if session is not None:
                    session.rollback()
            except Exception:
                pass
            logger.exception("[EXIT CLOSING RECONCILE] loop error")
        finally:
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass
        time.sleep(interval)


def start_exit_closing_stale_reconcile_loop() -> bool:
    """CLOSINGのまま長時間残った建玉を救済するバックグラウンドループを起動する (冪等)。"""
    global _exit_closing_reconcile_installed, _exit_closing_reconcile_thread_started
    if _exit_closing_reconcile_installed:
        return True
    if not EXIT_CLOSING_RECONCILE_ENABLED:
        logger.warning("[EXIT CLOSING RECONCILE] disabled")
        return False
    if not _exit_closing_reconcile_thread_started:
        threading.Thread(target=_exit_closing_reconcile_loop, daemon=True, name="exit_closing_reconcile_loop").start()
        _exit_closing_reconcile_thread_started = True
    _exit_closing_reconcile_installed = True
    logger.warning(
        "[EXIT CLOSING RECONCILE] installed stale_sec=%.1f interval=%.1f rest_timeout=%.1f memory_fallback=%s ignored=%s",
        _exit_board_env_float("EXIT_CLOSING_STALE_SEC", 20.0),
        _exit_board_env_float("EXIT_CLOSING_RECONCILE_INTERVAL_SEC", 5.0),
        _exit_board_env_float("EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC", 1.5),
        _exit_board_env_bool("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK", False),
        sorted(_exit_closing_reconcile_ignored_symbols()),
    )
    return True


# ============================================================
# exit_unfilled_reprice_runtime_patch (native化)
# 返済指値が未約定で残った場合、短時間で取消し、建玉をOPENへ戻して
# execute_exit() をもう一度呼ぶ。最終fallbackで成行を使う場合も、
# EXIT_REST_FULL_BOARD_ENABLED / EXIT_LIMIT_BOARD_TOUCH_ENABLED は
# その1回だけ一時的にOFFにし、呼び出し後に必ず復元する。
# ============================================================
_exit_unfilled_reprice_installed = False
_exit_unfilled_reprice_thread_started = False
_exit_unfilled_reprice_first_seen: dict[str, float] = {}
_exit_unfilled_reprice_cancel_sent: dict[str, float] = {}
_exit_unfilled_reprice_retry_rounds: dict[str, int] = {}
_exit_unfilled_reprice_lock = threading.RLock()


def _exit_unfilled_reprice_env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _exit_unfilled_reprice_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _exit_unfilled_reprice_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(float(v)) if v not in (None, "") else int(default)
    except Exception:
        return int(default)


def _exit_unfilled_reprice_f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _exit_unfilled_reprice_i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v)) if v not in (None, "") else int(default)
    except Exception:
        return int(default)


def _exit_unfilled_reprice_sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _exit_unfilled_reprice_remaining(order: dict[str, Any]) -> tuple[float, float, float]:
    qty = _exit_unfilled_reprice_f(order.get("OrderQty", order.get("Qty", 0)), 0.0)
    cum = _exit_unfilled_reprice_f(order.get("CumQty", order.get("ContractedQty", 0)), 0.0)
    leaves = _exit_unfilled_reprice_f(order.get("LeavesQty", 0), 0.0)
    if leaves <= 0 and qty > 0:
        leaves = max(qty - cum, 0.0)
    if qty <= 0 and leaves > 0:
        qty = leaves + cum
    return qty, cum, leaves


def _exit_unfilled_reprice_order_elapsed(order_id: str, order: dict[str, Any], now_ts: float) -> float:
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
    if order_id not in _exit_unfilled_reprice_first_seen:
        _exit_unfilled_reprice_first_seen[order_id] = now_ts
        logger.warning("[EXIT UNFILLED REPRICE] first_seen order_id=%s symbol=%s", order_id, order.get("Symbol"))
    return max(0.0, now_ts - _exit_unfilled_reprice_first_seen[order_id])


def _exit_unfilled_reprice_is_close_limit_order(order: dict[str, Any], cancelable_states: set[int]) -> bool:
    state = _exit_unfilled_reprice_i(order.get("State"), 0)
    price = _exit_unfilled_reprice_f(order.get("Price"), 0.0)
    front = _exit_unfilled_reprice_i(order.get("FrontOrderType"), 0)
    cash_margin = _exit_unfilled_reprice_i(order.get("CashMargin"), 0)
    qty, cum, leaves = _exit_unfilled_reprice_remaining(order)
    is_limit = front == 20 or (front != 10 and price > 0)
    is_open = leaves > 0 or (qty > 0 and cum < qty)
    is_close = cash_margin == 3
    return bool(is_close and is_limit and is_open and state in cancelable_states)


def _exit_unfilled_reprice_restore_position_open(symbol: str) -> None:
    symbol = _exit_unfilled_reprice_sym(symbol)
    try:
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
        pos = getattr(global_data, "open_positions", None)
        if isinstance(pos, dict):
            p = pos.get(symbol)
            if isinstance(p, dict):
                p["status"] = "OPEN"
                p["updated_at"] = dt.datetime.now()
    except Exception:
        logger.debug("[EXIT UNFILLED REPRICE] memory restore skipped", exc_info=True)


def _exit_unfilled_reprice_execute_with_optional_market(symbol: str, *, reason: str, exit_price: float, market_fallback: bool) -> None:
    if not market_fallback:
        execute_exit(symbol, reason=reason, exit_price=exit_price)
        return

    saved = {
        "EXIT_REST_FULL_BOARD_ENABLED": os.environ.get("EXIT_REST_FULL_BOARD_ENABLED"),
        "EXIT_LIMIT_BOARD_TOUCH_ENABLED": os.environ.get("EXIT_LIMIT_BOARD_TOUCH_ENABLED"),
    }
    try:
        os.environ["EXIT_REST_FULL_BOARD_ENABLED"] = "0"
        os.environ["EXIT_LIMIT_BOARD_TOUCH_ENABLED"] = "0"
        logger.warning("[EXIT UNFILLED REPRICE] temporary MARKET fallback enabled symbol=%s reason=%s", symbol, reason)
        execute_exit(symbol, reason=reason, exit_price=exit_price)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        logger.warning("[EXIT UNFILLED REPRICE] restored exit board env after MARKET fallback symbol=%s", symbol)


def _exit_unfilled_reprice_retry_exit(symbol: str, order: dict[str, Any]) -> None:
    symbol = _exit_unfilled_reprice_sym(symbol)
    if not symbol:
        return
    market_fallback = False
    with _exit_unfilled_reprice_lock:
        r = _exit_unfilled_reprice_retry_rounds.get(symbol, 0)
        max_rounds = _exit_unfilled_reprice_env_int("EXIT_UNFILLED_REPRICE_MAX_ROUNDS", 1)
        if r >= max_rounds:
            if _exit_unfilled_reprice_env_bool("EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL", True):
                market_fallback = True
                logger.warning("[EXIT UNFILLED REPRICE] final retry will use one-shot MARKET fallback symbol=%s round=%s", symbol, r)
            else:
                logger.warning("[EXIT UNFILLED REPRICE] max retry reached symbol=%s round=%s", symbol, r)
                return
        _exit_unfilled_reprice_retry_rounds[symbol] = r + 1

    try:
        ref_price = _exit_unfilled_reprice_f(order.get("Price"), 0.0)
        reason = f"unfilled_exit_reprice_round_{_exit_unfilled_reprice_retry_rounds.get(symbol, 0)}"
        if market_fallback:
            reason += "_market_fallback"
        logger.warning("[EXIT UNFILLED REPRICE] retry execute_exit symbol=%s ref_price=%s reason=%s market=%s", symbol, ref_price, reason, market_fallback)
        _exit_unfilled_reprice_execute_with_optional_market(symbol, reason=reason, exit_price=ref_price if ref_price > 0 else 1.0, market_fallback=market_fallback)
    except Exception:
        logger.exception("[EXIT UNFILLED REPRICE] retry execute_exit failed symbol=%s", symbol)


def _exit_unfilled_reprice_loop() -> None:
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

    interval = max(0.2, _exit_unfilled_reprice_env_float("EXIT_UNFILLED_CHECK_INTERVAL_SEC", 0.5))
    cancel_after = max(0.5, _exit_unfilled_reprice_env_float("EXIT_UNFILLED_CANCEL_SEC", 1.2))
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
                if not _exit_unfilled_reprice_is_close_limit_order(order, cancelable_states):
                    continue
                symbol = _exit_unfilled_reprice_sym(order.get("Symbol"))
                qty, cum, leaves = _exit_unfilled_reprice_remaining(order)
                elapsed = _exit_unfilled_reprice_order_elapsed(order_id, order, now_ts)
                logger.warning("[EXIT UNFILLED REPRICE] watch order_id=%s symbol=%s elapsed=%.2fs remain=%.0f qty=%.0f cum=%.0f price=%s", order_id, symbol, elapsed, leaves, qty, cum, order.get("Price"))
                if elapsed < cancel_after:
                    continue
                if now_ts - _exit_unfilled_reprice_cancel_sent.get(order_id, 0.0) < 1.0:
                    continue
                logger.warning("[EXIT UNFILLED REPRICE] cancel close order_id=%s symbol=%s elapsed=%.2fs", order_id, symbol, elapsed)
                cancel_order(order_id)
                _exit_unfilled_reprice_cancel_sent[order_id] = now_ts
                _exit_unfilled_reprice_restore_position_open(symbol)
                if _exit_unfilled_reprice_env_bool("EXIT_UNFILLED_REPRICE_ENABLED", True):
                    _exit_unfilled_reprice_retry_exit(symbol, order)
                time.sleep(0.1)
            for oid in list(_exit_unfilled_reprice_first_seen.keys()):
                if oid not in active:
                    _exit_unfilled_reprice_first_seen.pop(oid, None)
                    _exit_unfilled_reprice_cancel_sent.pop(oid, None)
        except Exception:
            logger.exception("[EXIT UNFILLED REPRICE] loop error")
        time.sleep(interval)


def start_exit_unfilled_reprice_loop() -> bool:
    """返済指値未約定の取消・再発注 (一時MARKET fallback込み) ループを起動する (冪等)。"""
    global _exit_unfilled_reprice_installed, _exit_unfilled_reprice_thread_started
    if _exit_unfilled_reprice_installed:
        return True
    if not _exit_unfilled_reprice_env_bool("EXIT_UNFILLED_REPRICE_ENABLED", True):
        logger.warning("[EXIT UNFILLED REPRICE] disabled")
        return False
    if not _exit_unfilled_reprice_thread_started:
        threading.Thread(target=_exit_unfilled_reprice_loop, daemon=True, name="exit_unfilled_reprice_loop").start()
        _exit_unfilled_reprice_thread_started = True
    _exit_unfilled_reprice_installed = True
    logger.warning(
        "[EXIT UNFILLED REPRICE] installed cancel_sec=%.2f max_rounds=%s market_on_final=%s temp_market=True",
        _exit_unfilled_reprice_env_float("EXIT_UNFILLED_CANCEL_SEC", 1.2),
        _exit_unfilled_reprice_env_int("EXIT_UNFILLED_REPRICE_MAX_ROUNDS", 1),
        _exit_unfilled_reprice_env_bool("EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL", True),
    )
    return True


__all__ = [
    "set_exit_order_sender",
    "execute_exit",
    "start_exit_fill_confirm_loop",
    "start_exit_closing_stale_reconcile_loop",
    "start_exit_unfilled_reprice_loop",
]


# ============================================================
# broker 実建玉フォールバック (EXIT)
# ------------------------------------------------------------
# 旧 core/startup/exit_executor_broker_position_patch.py
# (V1.0-BROKER-CREDIT-FALLBACK) を本文化。
#
#   - global_data.open_positions / GC.positions で建玉が見つからない場合に
#     kabu Station の信用建玉から直接復元して EXIT 注文へ進ませる
#   - broker 実建玉を正とするため、返済注文の受付後に positions.db へ
#     行が無くても EXIT 失敗扱いにしない
#
# 適用スコープは旧 install 導線 (fast_startup_runtime_patch <- main.py) を
# _broker_position_fallback_enabled() で再現する。
# 記号正規化は本ファイル既存の _normalize_symbol と実装が異なるため、
# 旧パッチのものを _bkp_normalize_symbol として別に移植した (流用しない)。
# ============================================================

_BROKER_POS_FALLBACK_SCOPE: Optional[bool] = None


def _broker_position_fallback_enabled() -> bool:
    global _BROKER_POS_FALLBACK_SCOPE
    if _BROKER_POS_FALLBACK_SCOPE is not None:
        return _BROKER_POS_FALLBACK_SCOPE
    enabled = False
    try:
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        db_names = (
            "main_database.py", "data_collectors_runner.py", "db_prepare_runner.py",
            "push_receiver_runner.py", "yahoo_complement_runner.py",
            "summary_database_runner.py", "ranking_collector_runner.py",
        )
        if not any(n in argv_text for n in db_names):
            enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _BROKER_POS_FALLBACK_SCOPE = enabled
    return enabled


def _bkp_normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _bkp_ensure_global_open_positions() -> dict[str, Any]:
    try:
        from global_state import global_data

        d = getattr(global_data, "open_positions", None)
        if isinstance(d, dict):
            return d
        d = {}
        setattr(global_data, "open_positions", d)
        return d
    except Exception:
        return {}


def _bkp_read_broker_position(symbol: str) -> Optional[dict[str, Any]]:
    symbol = _bkp_normalize_symbol(symbol)
    if not symbol:
        return None
    try:
        from trading.position.kabu_position_reader import read_kabu_open_positions

        rows = read_kabu_open_positions() or {}
        pos = rows.get(symbol) or rows.get(str(symbol))
        if isinstance(pos, dict):
            pos.setdefault("symbol", symbol)
            pos.setdefault("status", "OPEN")
            pos.setdefault("_position_source", "KABU.positions.credit_only.direct_exit_fallback")
            return pos
    except Exception:
        logger.warning("[EXIT EXECUTOR BROKER PATCH] broker position read failed symbol=%s", symbol, exc_info=True)
    return None


def _bkp_is_broker_credit_memory_position(symbol: str) -> bool:
    symbol = _bkp_normalize_symbol(symbol)
    try:
        positions = _bkp_ensure_global_open_positions()
        pos = positions.get(symbol) or positions.get(str(symbol))
        if not isinstance(pos, dict):
            return False
        src = str(pos.get("_position_source") or "").upper()
        return src.startswith("KABU.POSITIONS") or "CREDIT_ONLY" in src
    except Exception:
        return False


def _get_open_position(symbol: str) -> tuple[Optional[dict[str, Any]], str]:
    pos, source = _get_open_position_core(symbol)
    if pos is not None or not _broker_position_fallback_enabled():
        return pos, source

    s = _bkp_normalize_symbol(symbol)
    broker_pos = _bkp_read_broker_position(s)
    if isinstance(broker_pos, dict):
        positions = _bkp_ensure_global_open_positions()
        positions[s] = broker_pos
        logger.warning(
            "[EXIT EXECUTOR BROKER PATCH] position recovered from broker credit positions symbol=%s",
            s,
        )
        return broker_pos, "KABU.positions.credit_only.direct_exit_fallback"

    return None, ""


def _close_db_position(*, symbol: str, exit_price: float, pnl: float, reason: str, order_id: str) -> bool:
    ok = _close_db_position_core(
        symbol=symbol,
        exit_price=exit_price,
        pnl=pnl,
        reason=reason,
        order_id=order_id,
    )
    if ok:
        return True
    if not _broker_position_fallback_enabled():
        return False

    # broker実建玉から返済した場合、positions.dbに行が無いことがある。
    # 注文はすでに受け付け後なので、DB未存在だけでEXIT失敗扱いにしない。
    if _bkp_is_broker_credit_memory_position(symbol):
        logger.warning(
            "[EXIT EXECUTOR BROKER PATCH] DB close missing ignored for broker credit position symbol=%s order_id=%s",
            symbol,
            order_id,
        )
        return True

    return False


# 旧 core/startup/exit_limit_pending_close_runtime_patch.py が付けていた
# _exit_limit_pending_close_wrapped マーカの後継 (本文化済み)。
# BOARD RUNTIME SELF CHECK 用。
_close_db_position._exit_limit_pending_close_builtin = True  # type: ignore[attr-defined]
