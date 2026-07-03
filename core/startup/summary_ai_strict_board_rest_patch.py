# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-SUMMARY-AI-STRICT-REST-BOARD-FALLBACK-RETRY-HOOK"
_INSTALLED = False
_ORIG_GET_LATEST_BID_ASK = None
_ORIG_GET_BOARD_WITH_RETRY = None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _extract_bid_ask(board: Any) -> tuple[float, float, float, float]:
    if not isinstance(board, dict):
        return 0.0, 0.0, 0.0, 0.0
    buy1 = board.get("Buy1") if isinstance(board.get("Buy1"), dict) else {}
    sell1 = board.get("Sell1") if isinstance(board.get("Sell1"), dict) else {}
    bid = _safe_float(board.get("bid_price") or board.get("bid") or board.get("best_bid") or board.get("BidPrice") or board.get("BestBid") or buy1.get("Price"), 0.0)
    ask = _safe_float(board.get("ask_price") or board.get("ask") or board.get("best_ask") or board.get("AskPrice") or board.get("BestAsk") or sell1.get("Price"), 0.0)
    bid_qty = _safe_float(board.get("bid_qty") or board.get("BidQty") or board.get("bid_volume") or board.get("BestBidQty") or buy1.get("Qty"), 0.0)
    ask_qty = _safe_float(board.get("ask_qty") or board.get("AskQty") or board.get("ask_volume") or board.get("BestAskQty") or sell1.get("Qty"), 0.0)
    return bid, ask, bid_qty, ask_qty


def _valid_board(board: Any) -> bool:
    try:
        bid, ask, _, _ = _extract_bid_ask(board)
        return bid > 0 and ask > 0
    except Exception:
        return False


def _summary_like(source: str) -> bool:
    s = str(source or "").strip().upper()
    return s in {"SUMMARY_AI", "SUMMARY", "PUSH_SUMMARY", "STOCK_SUMMARY", "PUSH", "SUMMARY_AI_ORDER_BUILDER"}


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _infer_source_from_stack() -> tuple[str, str]:
    """Infer source/side from entry_order_builder frames.

    entry_order_builder._get_board_with_retry historically calls
    get_latest_bid_ask(symbol) without kwargs, so source/side are not available
    at the patched get_latest_bid_ask layer. This helper reads caller locals.
    """
    source = ""
    side = ""
    try:
        for frame in inspect.stack(context=0)[:12]:
            fn = str(getattr(frame, "function", ""))
            filename = str(getattr(frame, "filename", "")).replace("\\", "/").lower()
            loc = getattr(frame, "frame", None).f_locals if getattr(frame, "frame", None) is not None else {}
            if filename.endswith("trading/handlers/entry_order_builder.py"):
                if fn in {"_get_board_with_retry", "build_entry_order"}:
                    source = str(loc.get("source") or loc.get("entry_source") or source or "SUMMARY_AI_ORDER_BUILDER")
                    side = _norm_side(loc.get("side") or loc.get("entry_side") or side)
                    break
            if not side:
                side = _norm_side(loc.get("side") or loc.get("entry_side") or loc.get("ai_side"))
            if not source:
                source = str(loc.get("source") or loc.get("entry_source") or "")
    except Exception:
        pass
    return (source or "", side or "")


def _fetch_rest_board(symbol: Any, *, side: str = "", source: str = "") -> Any:
    try:
        from core.startup import board_retry_patch as brp
        old = os.environ.get("ENTRY_BOARD_REST_DIRECT_ENABLED")
        os.environ["ENTRY_BOARD_REST_DIRECT_ENABLED"] = "1"
        try:
            rest = brp._fetch_board_rest(str(symbol), side=side, source=source or "SUMMARY_AI_ORDER_BUILDER")
        finally:
            if old is None:
                os.environ.pop("ENTRY_BOARD_REST_DIRECT_ENABLED", None)
            else:
                os.environ["ENTRY_BOARD_REST_DIRECT_ENABLED"] = old
        return rest
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] REST fetch failed symbol=%s side=%s source=%s version=%s", symbol, side, source, VERSION)
        return None


def _board_or_rest(board: Any, symbol: Any, *, side: str = "", source: str = "", inferred: str = "") -> Any:
    if _valid_board(board):
        return board
    if not source:
        stack_source, stack_side = _infer_source_from_stack()
        source = stack_source or inferred
        side = side or stack_side
    source_u = str(source or "").strip().upper()
    if not _summary_like(source_u):
        return board
    rest = _fetch_rest_board(symbol, side=side, source=source_u or "SUMMARY_AI_ORDER_BUILDER")
    if _valid_board(rest):
        logger.warning(
            "[SUMMARY AI STRICT BOARD REST] board recovered symbol=%s side=%s source=%s inferred=%s version=%s",
            symbol, side, source_u, inferred, VERSION,
        )
        return rest
    logger.warning(
        "[SUMMARY AI STRICT BOARD REST] board still missing symbol=%s side=%s source=%s inferred=%s version=%s",
        symbol, side, source_u, inferred, VERSION,
    )
    return board


def _unwrap_original(fn: Any) -> Any:
    seen: set[int] = set()
    cur = fn
    while callable(cur) and id(cur) not in seen:
        seen.add(id(cur))
        nxt = getattr(cur, "_original", None)
        if not callable(nxt) or nxt is cur:
            break
        cur = nxt
    return cur


def install() -> bool:
    global _INSTALLED, _ORIG_GET_LATEST_BID_ASK, _ORIG_GET_BOARD_WITH_RETRY
    if _INSTALLED:
        return True
    try:
        from trading.handlers import entry_order_builder as eob

        cur_get = getattr(eob, "get_latest_bid_ask", None)
        if callable(cur_get) and not getattr(cur_get, "_summary_ai_strict_board_rest_v3", False):
            _ORIG_GET_LATEST_BID_ASK = _unwrap_original(cur_get)

            @wraps(_ORIG_GET_LATEST_BID_ASK)
            def _patched_get_latest_bid_ask(symbol: Any, *args: Any, **kwargs: Any):
                source = str(kwargs.get("source") or "").strip().upper()
                side = _norm_side(kwargs.get("side"))
                inferred = ""
                if not source:
                    inferred_source, inferred_side = _infer_source_from_stack()
                    inferred = inferred_source
                    source = inferred_source
                    side = side or inferred_side
                try:
                    board = _ORIG_GET_LATEST_BID_ASK(symbol, *args, **kwargs)
                except TypeError:
                    board = _ORIG_GET_LATEST_BID_ASK(symbol)
                return _board_or_rest(board, symbol, side=side, source=source, inferred=inferred)

            _patched_get_latest_bid_ask._summary_ai_strict_board_rest_v1 = True  # type: ignore[attr-defined]
            _patched_get_latest_bid_ask._summary_ai_strict_board_rest_v2 = True  # type: ignore[attr-defined]
            _patched_get_latest_bid_ask._summary_ai_strict_board_rest_v3 = True  # type: ignore[attr-defined]
            _patched_get_latest_bid_ask._original = _ORIG_GET_LATEST_BID_ASK  # type: ignore[attr-defined]
            eob.get_latest_bid_ask = _patched_get_latest_bid_ask

        cur_retry = getattr(eob, "_get_board_with_retry", None)
        if callable(cur_retry) and not getattr(cur_retry, "_summary_ai_strict_board_retry_v3", False):
            _ORIG_GET_BOARD_WITH_RETRY = _unwrap_original(cur_retry)

            @wraps(_ORIG_GET_BOARD_WITH_RETRY)
            def _patched_get_board_with_retry(symbol: Any, *args: Any, **kwargs: Any):
                source = str(kwargs.get("source") or "").strip().upper()
                side = _norm_side(kwargs.get("side"))
                try:
                    board = _ORIG_GET_BOARD_WITH_RETRY(symbol, *args, **kwargs)
                except TypeError:
                    board = _ORIG_GET_BOARD_WITH_RETRY(symbol)
                return _board_or_rest(board, symbol, side=side, source=source, inferred="_get_board_with_retry")

            _patched_get_board_with_retry._summary_ai_strict_board_retry_v3 = True  # type: ignore[attr-defined]
            _patched_get_board_with_retry._original = _ORIG_GET_BOARD_WITH_RETRY  # type: ignore[attr-defined]
            eob._get_board_with_retry = _patched_get_board_with_retry

        _INSTALLED = True
        logger.warning("[SUMMARY AI STRICT BOARD REST] installed version=%s get_latest=%s retry_hook=%s", VERSION, callable(cur_get), callable(cur_retry))
        return True
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI STRICT BOARD REST] auto install failed")


__all__ = ["install", "VERSION"]
