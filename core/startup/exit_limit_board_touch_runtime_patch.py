# ============================================================
# File   : core/startup/exit_limit_board_touch_runtime_patch.py
# Version: V1.1-EXIT-REST-FULL-BOARD-LIMIT
# ------------------------------------------------------------
# 目的:
#   EXIT返済注文を成行固定から、板タッチ指値へ変更する。
#   REST /board が使える場合は複数段板を見て返済価格を補正する。
#
# 方式:
#   BUY建玉返済  close_side=SELL -> 原則 bid 指値。REST板有効時は厚い買い板側へ寄せる。
#   SELL建玉返済 close_side=BUY  -> 原則 ask 指値。REST板有効時は厚い売り板側へ寄せる。
#
# 注意:
#   REST板が取れない場合は既存のPUSH最良bid/askへfallback。
#   bid/askも取れない場合は、既定では従来通り成行にfallbackする。
#
# 環境変数:
#   EXIT_LIMIT_BOARD_TOUCH_ENABLED=1
#   EXIT_REST_FULL_BOARD_ENABLED=1
#   EXIT_REST_FULL_BOARD_EXCHANGE=1
#   EXIT_REST_FULL_BOARD_DEPTH=10
#   EXIT_REST_FULL_BOARD_THICK_MIN_QTY=500
#   EXIT_REST_FULL_BOARD_CACHE_SEC=0.8
#   EXIT_REST_FULL_BOARD_MIN_INTERVAL_SEC=0.7
#   EXIT_REST_FULL_BOARD_TIMEOUT_SEC=0.8
#   EXIT_LIMIT_FALLBACK_MARKET_IF_NO_BOARD=1
# ============================================================

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_CACHE: dict[str, tuple[float, dict]] = {}
_LAST_CALL: dict[str, float] = {}


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
        if v is None or v == "":
            return float(default)
        x = float(v)
        return float(default) if math.isnan(x) or math.isinf(x) else x
    except Exception:
        return float(default)


def _get_bid_ask(symbol: str) -> tuple[float, float]:
    try:
        from utils_common import get_latest_bid_ask
        ret = get_latest_bid_ask(str(symbol))
        if isinstance(ret, dict):
            bid = _f(ret.get("bid") or ret.get("bid_price") or ret.get("BidPrice") or ret.get("best_bid"), 0.0)
            ask = _f(ret.get("ask") or ret.get("ask_price") or ret.get("AskPrice") or ret.get("best_ask"), 0.0)
            return bid, ask
        if isinstance(ret, (tuple, list)) and len(ret) >= 2:
            return _f(ret[0], 0.0), _f(ret[1], 0.0)
    except Exception:
        logger.debug("[EXIT LIMIT BOARD TOUCH] utils_common.get_latest_bid_ask failed symbol=%s", symbol, exc_info=True)

    try:
        from global_state import global_data
        tick = global_data.get_latest_tick(str(symbol))
        if isinstance(tick, dict):
            bid = _f(tick.get("BidPrice") or tick.get("bid") or tick.get("best_bid"), 0.0)
            ask = _f(tick.get("AskPrice") or tick.get("ask") or tick.get("best_ask"), 0.0)
            return bid, ask
    except Exception:
        logger.debug("[EXIT LIMIT BOARD TOUCH] global tick bid/ask failed symbol=%s", symbol, exc_info=True)

    return 0.0, 0.0


def _get_token() -> str | None:
    try:
        from token_manager import get_valid_token, refresh_token
        return get_valid_token() or refresh_token()
    except Exception:
        logger.debug("[EXIT REST FULL BOARD] token unavailable", exc_info=True)
        return None


def _fetch_rest_board(symbol: str, exchange: int) -> dict | None:
    if not _env_bool("EXIT_REST_FULL_BOARD_ENABLED", True):
        return None

    now = time.monotonic()
    cache_sec = _env_float("EXIT_REST_FULL_BOARD_CACHE_SEC", 0.8)
    cached = _CACHE.get(symbol)
    if cached and now - cached[0] <= cache_sec:
        out = dict(cached[1])
        out["cache_hit"] = True
        return out

    min_interval = _env_float("EXIT_REST_FULL_BOARD_MIN_INTERVAL_SEC", 0.7)
    if cached and now - _LAST_CALL.get(symbol, 0.0) < min_interval:
        out = dict(cached[1])
        out["cache_hit"] = True
        out["rate_limited_cache"] = True
        return out

    token = _get_token()
    if not token:
        return None

    base = os.getenv("KABUSAPI_BASE_URL", "http://localhost:18080/kabusapi").rstrip("/")
    ex = str(os.getenv("EXIT_REST_FULL_BOARD_EXCHANGE", str(exchange or 1)) or exchange or 1)
    url = f"{base}/board/{urllib.parse.quote(str(symbol) + '@' + ex)}"
    req = urllib.request.Request(url, headers={"X-API-KEY": token}, method="GET")
    _LAST_CALL[symbol] = now
    try:
        with urllib.request.urlopen(req, timeout=_env_float("EXIT_REST_FULL_BOARD_TIMEOUT_SEC", 0.8)) as res:
            raw = json.loads(res.read().decode("utf-8", errors="replace"))
    except Exception:
        logger.debug("[EXIT REST FULL BOARD] request failed symbol=%s", symbol, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None

    depth = int(_env_float("EXIT_REST_FULL_BOARD_DEPTH", 10))
    bids, asks = [], []
    for i in range(1, max(1, depth) + 1):
        b = raw.get(f"Buy{i}") if isinstance(raw.get(f"Buy{i}"), dict) else {}
        a = raw.get(f"Sell{i}") if isinstance(raw.get(f"Sell{i}"), dict) else {}
        bp, bq = _f(b.get("Price")), _f(b.get("Qty"))
        ap, aq = _f(a.get("Price")), _f(a.get("Qty"))
        if bp > 0 and bq > 0:
            bids.append({"level": i, "price": bp, "qty": bq})
        if ap > 0 and aq > 0:
            asks.append({"level": i, "price": ap, "qty": aq})

    bid = _f(raw.get("BidPrice")) or (bids[0]["price"] if bids else 0.0)
    ask = _f(raw.get("AskPrice")) or (asks[0]["price"] if asks else 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    board = {"bid": bid, "ask": ask, "bids": bids, "asks": asks, "source": "rest_board"}
    _CACHE[symbol] = (now, board)
    return dict(board)


def _get_tick_size(price: float) -> float:
    try:
        from utils_common import get_tick_size
        t = float(get_tick_size(price))
        return t if t > 0 else 1.0
    except Exception:
        return 1.0


def _round_price(price: float, close_side: str) -> float:
    t = _get_tick_size(price)
    if str(close_side or "").upper() == "BUY":
        return math.ceil(price / t) * t
    return math.floor(price / t) * t


def _thick(levels: list[dict], fallback_price: float) -> dict:
    min_qty = _env_float("EXIT_REST_FULL_BOARD_THICK_MIN_QTY", 500.0)
    usable = [x for x in levels if _f(x.get("qty")) >= min_qty]
    src = usable or levels or [{"level": 1, "price": fallback_price, "qty": 0.0}]
    return max(src, key=lambda x: _f(x.get("qty")))


def _sum_qty(levels: list[dict], n: int) -> float:
    return sum(_f(x.get("qty")) for x in levels[: max(1, n)])


def _rest_exit_limit_price(symbol: str, close_side: str, exchange: int) -> tuple[float, str, dict] | None:
    board = _fetch_rest_board(str(symbol), exchange)
    if not board:
        return None

    bid, ask = _f(board.get("bid")), _f(board.get("ask"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    max_spread = _env_float("EXIT_REST_FULL_BOARD_MAX_SPREAD_PCT", _env_float("ENTRY_MAX_SPREAD_PCT", 0.15))
    if spread_pct > max_spread and _env_bool("EXIT_REST_FULL_BOARD_STRICT_SPREAD", False):
        logger.warning(
            "[EXIT REST FULL BOARD] spread too wide -> fallback symbol=%s close_side=%s bid=%s ask=%s spread=%.4f max=%.4f",
            symbol, close_side, bid, ask, spread_pct, max_spread,
        )
        return None

    side = str(close_side or "").upper()
    t = _get_tick_size(mid)
    bids, asks = list(board.get("bids") or []), list(board.get("asks") or [])
    n = int(_env_float("EXIT_REST_FULL_BOARD_IMBALANCE_DEPTH", 5))

    if side == "SELL":
        # BUY建玉の返済売り。厚い買い板の価格を参考にしつつ、原則bidタッチで逃げる。
        lv = _thick(bids, bid)
        support = _f(lv.get("price"), bid)
        price = max(min(bid, support), support)
        mode = "EXIT_REST_SELL_CLOSE_THICK_BID"
    elif side == "BUY":
        # SELL建玉の返済買い。厚い売り板の価格を参考にしつつ、原則askタッチで逃げる。
        lv = _thick(asks, ask)
        resistance = _f(lv.get("price"), ask)
        price = min(max(ask, resistance), resistance)
        mode = "EXIT_REST_BUY_CLOSE_THICK_ASK"
    else:
        return None

    # 急ぐ返済なので、厚い板が遠すぎても現在のbest bid/askから離しすぎない。
    max_ticks_away = max(0, int(_env_float("EXIT_REST_FULL_BOARD_MAX_TICKS_AWAY", 0)))
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
        "bid_total_topN": _sum_qty(bids, n),
        "ask_total_topN": _sum_qty(asks, n),
        "cache_hit": bool(board.get("cache_hit")),
    }
    return _round_price(price, side), mode, meta


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.exit.executor as ex
    except Exception:
        logger.exception("[EXIT LIMIT BOARD TOUCH] import trading.exit.executor failed")
        return False

    old_builder = getattr(ex, "_build_kabu_close_payload", None)
    if not callable(old_builder):
        logger.warning("[EXIT LIMIT BOARD TOUCH] _build_kabu_close_payload not callable")
        return False

    if not getattr(old_builder, "_exit_limit_board_touch_wrapped_v11", False):
        def wrapped_build_kabu_close_payload(*, symbol: str, close_side: str, qty: float, exchange: int = 1):
            payload = old_builder(symbol=symbol, close_side=close_side, qty=qty, exchange=exchange)
            if not _env_bool("EXIT_LIMIT_BOARD_TOUCH_ENABLED", True):
                return payload

            close_side_u = str(close_side or "").upper()
            limit_price = 0.0
            rule = ""
            rest_meta = None

            rest_decision = _rest_exit_limit_price(str(symbol), close_side_u, int(exchange or 1))
            if rest_decision is not None:
                limit_price, rule, rest_meta = rest_decision
            else:
                bid, ask = _get_bid_ask(str(symbol))
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

            if _env_bool("EXIT_LIMIT_FALLBACK_MARKET_IF_NO_BOARD", True):
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

        wrapped_build_kabu_close_payload._exit_limit_board_touch_wrapped_v11 = True  # type: ignore[attr-defined]
        wrapped_build_kabu_close_payload._original = old_builder  # type: ignore[attr-defined]
        ex._build_kabu_close_payload = wrapped_build_kabu_close_payload

    _INSTALLED = True
    logger.warning("[EXIT LIMIT BOARD TOUCH] installed REST full board first, PUSH best bid/ask fallback")
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT LIMIT BOARD TOUCH] auto install failed")

__all__ = ["install"]
