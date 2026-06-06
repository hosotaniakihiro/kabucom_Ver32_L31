# ============================================================
# File   : core/startup/rest_full_board_entry_patch.py
# Version: V1-CANDIDATE-ONLY
# ------------------------------------------------------------
# build_entry_order() が作った LIMIT 注文だけ、kabu Station REST
# /board の複数段板で最終価格を補正する。
# 常時監視銘柄にはRESTを叩かず、エントリー候補だけに限定する。
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


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if not math.isnan(x) and not math.isinf(x) else float(default)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _source_enabled(source: str) -> bool:
    if not _env_bool("ENTRY_REST_FULL_BOARD_ENABLED", True):
        return False
    src = str(source or "").upper()
    allowed = {x.strip().upper() for x in os.getenv("ENTRY_REST_FULL_BOARD_SOURCES", "SUMMARY_AI,RANKING,TONOSAMA,EARLY_SCALP,ENTRY").split(",") if x.strip()}
    return "ALL" in allowed or src in allowed


def _tick(price: float) -> float:
    try:
        from utils_common import get_tick_size
        t = float(get_tick_size(price))
        return t if t > 0 else 1.0
    except Exception:
        return 1.0


def _round(price: float, side: str) -> float:
    t = _tick(price)
    return math.ceil(price / t) * t if side == "BUY" else math.floor(price / t) * t


def _get_token() -> str | None:
    try:
        from token_manager import get_valid_token, refresh_token
        return get_valid_token() or refresh_token()
    except Exception:
        logger.debug("[REST FULL BOARD] token unavailable", exc_info=True)
        return None


def _fetch_board(symbol: str) -> dict | None:
    now = time.monotonic()
    cache_sec = _env_float("ENTRY_REST_FULL_BOARD_CACHE_SEC", 0.8)
    cached = _CACHE.get(symbol)
    if cached and now - cached[0] <= cache_sec:
        out = dict(cached[1])
        out["cache_hit"] = True
        return out

    min_interval = _env_float("ENTRY_REST_FULL_BOARD_MIN_INTERVAL_SEC", 0.7)
    if cached and now - _LAST_CALL.get(symbol, 0.0) < min_interval:
        out = dict(cached[1])
        out["cache_hit"] = True
        out["rate_limited_cache"] = True
        return out

    token = _get_token()
    if not token:
        return None
    base = os.getenv("KABUSAPI_BASE_URL", "http://localhost:18080/kabusapi").rstrip("/")
    exchange = os.getenv("ENTRY_REST_FULL_BOARD_EXCHANGE", "1")
    url = f"{base}/board/{urllib.parse.quote(symbol + '@' + exchange)}"
    req = urllib.request.Request(url, headers={"X-API-KEY": token}, method="GET")
    _LAST_CALL[symbol] = now
    try:
        with urllib.request.urlopen(req, timeout=_env_float("ENTRY_REST_FULL_BOARD_TIMEOUT_SEC", 0.8)) as res:
            raw = json.loads(res.read().decode("utf-8", errors="replace"))
    except Exception:
        logger.debug("[REST FULL BOARD] request failed symbol=%s", symbol, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None

    depth = int(_env_float("ENTRY_REST_FULL_BOARD_DEPTH", 10))
    bids, asks = [], []
    for i in range(1, max(1, depth) + 1):
        b = raw.get(f"Buy{i}") if isinstance(raw.get(f"Buy{i}"), dict) else {}
        a = raw.get(f"Sell{i}") if isinstance(raw.get(f"Sell{i}"), dict) else {}
        bp, bq = _safe_float(b.get("Price")), _safe_float(b.get("Qty"))
        ap, aq = _safe_float(a.get("Price")), _safe_float(a.get("Qty"))
        if bp > 0 and bq > 0:
            bids.append({"level": i, "price": bp, "qty": bq})
        if ap > 0 and aq > 0:
            asks.append({"level": i, "price": ap, "qty": aq})

    bid = _safe_float(raw.get("BidPrice")) or (bids[0]["price"] if bids else 0.0)
    ask = _safe_float(raw.get("AskPrice")) or (asks[0]["price"] if asks else 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    board = {"bid": bid, "ask": ask, "bids": bids, "asks": asks, "source": "rest_board"}
    _CACHE[symbol] = (now, board)
    return dict(board)


def _sum_qty(levels: list[dict], n: int) -> float:
    return sum(_safe_float(x.get("qty")) for x in levels[: max(1, n)])


def _thick(levels: list[dict], fallback_price: float) -> dict:
    min_qty = _env_float("ENTRY_REST_FULL_BOARD_THICK_MIN_QTY", 500.0)
    usable = [x for x in levels if _safe_float(x.get("qty")) >= min_qty]
    src = usable or levels or [{"level": 1, "price": fallback_price, "qty": 0.0}]
    return max(src, key=lambda x: _safe_float(x.get("qty")))


def _decide_price(board: dict, side: str) -> tuple[float, str, dict]:
    bid, ask = _safe_float(board.get("bid")), _safe_float(board.get("ask"))
    mid = (bid + ask) / 2.0
    t = _tick(mid)
    bids, asks = list(board.get("bids") or []), list(board.get("asks") or [])
    touch = _env_bool("ENTRY_REST_FULL_BOARD_ALLOW_TOUCH_ASK_BID", True)
    if side == "BUY":
        lv = _thick(bids, bid)
        p = max(_safe_float(lv.get("price")) + t, bid + t)
        p = min(p, ask if touch else max(bid, ask - t))
        mode = "REST_FULL_BOARD_BUY_THICK_BID_PLUS_TICK"
    else:
        lv = _thick(asks, ask)
        p = min(_safe_float(lv.get("price")) - t, ask - t)
        p = max(p, bid if touch else min(ask, bid + t))
        mode = "REST_FULL_BOARD_SELL_THICK_ASK_MINUS_TICK"
    n = int(_env_float("ENTRY_REST_FULL_BOARD_IMBALANCE_DEPTH", 5))
    return _round(p, side), mode, {"bid": bid, "ask": ask, "tick": t, "thick_level": lv, "bid_total_topN": _sum_qty(bids, n), "ask_total_topN": _sum_qty(asks, n), "depth": n}


def _patch_result(ret: dict, *, symbol: str, side: str, source: str) -> dict:
    if not isinstance(ret, dict) or not ret.get("ok") or not _source_enabled(source):
        return ret
    detail = ret.get("detail")
    if not isinstance(detail, dict) or str(detail.get("order_type") or "").upper() != "LIMIT":
        return ret
    sym, side = _norm_symbol(symbol), _norm_side(side)
    if side not in {"BUY", "SELL"} or not sym:
        return ret
    board = _fetch_board(sym)
    if not board:
        detail["rest_full_board"] = False
        detail["rest_full_board_reason"] = "missing_keep_existing"
        return ret
    bid, ask = _safe_float(board.get("bid")), _safe_float(board.get("ask"))
    spread_pct = ((ask - bid) / ((ask + bid) / 2.0)) * 100.0
    max_spread = _env_float("ENTRY_REST_FULL_BOARD_MAX_SPREAD_PCT", _env_float("ENTRY_MAX_SPREAD_PCT", 0.15))
    if spread_pct > max_spread and _env_bool("ENTRY_REST_FULL_BOARD_STRICT_GUARD", True):
        return {"ok": False, "reason": "REST_FULL_BOARD_SPREAD_TOO_WIDE", "detail": {**detail, "bid": bid, "ask": ask, "spread_pct": spread_pct, "max_spread_pct": max_spread}}
    old = _safe_float(detail.get("price"))
    new, mode, meta = _decide_price(board, side)
    if new <= 0:
        return ret
    detail["price_before_rest_full_board"] = old
    detail["price"] = new
    detail["base_price"] = new
    detail["price_source"] = "rest_full_board_price"
    detail["rest_full_board"] = True
    detail["rest_full_board_mode"] = mode
    detail["rest_full_board_meta"] = {**meta, "spread_pct": spread_pct}
    logger.warning("[REST FULL BOARD] symbol=%s side=%s source=%s mode=%s old=%s new=%s meta=%s cache=%s", sym, side, source, mode, old, new, detail["rest_full_board_meta"], bool(board.get("cache_hit")))
    return ret


def install() -> bool:
    global _INSTALLED
    if not _env_bool("ENTRY_REST_FULL_BOARD_ENABLED", True):
        logger.warning("[REST FULL BOARD] disabled")
        return False
    try:
        import trading.handlers.entry_controller as ec
        import trading.handlers.entry_order_builder as eob
        old = getattr(eob, "build_entry_order", None)
        if not callable(old):
            return False
        if getattr(old, "_rest_full_board_patch", False):
            return True

        def wrapped(*args: Any, **kwargs: Any) -> dict:
            ret = old(*args, **kwargs)
            try:
                return _patch_result(ret, symbol=kwargs.get("symbol", ""), side=kwargs.get("side", ""), source=kwargs.get("source", ""))
            except Exception:
                logger.exception("[REST FULL BOARD] patch failed; keep original")
                return ret

        wrapped._rest_full_board_patch = True  # type: ignore[attr-defined]
        wrapped._original = old  # type: ignore[attr-defined]
        eob.build_entry_order = wrapped
        ec.build_entry_order = wrapped
        _INSTALLED = True
        logger.warning("[REST FULL BOARD] installed candidate-only REST /board price patch")
        return True
    except Exception:
        logger.exception("[REST FULL BOARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[REST FULL BOARD] auto install failed")


__all__ = ["install"]
