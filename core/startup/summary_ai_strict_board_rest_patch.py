# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V11-SUMMARY-AI-REST-BOARD-ATR-LIQ-BRIDGE-HARD-BLOCK"
_INSTALLED = False
_ORIG_GET_LATEST_BID_ASK = None
_ORIG_GET_BOARD_WITH_RETRY = None


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _extract_bid_ask(board: Any) -> tuple[float, float, float, float]:
    if isinstance(board, (tuple, list)) and len(board) >= 2:
        return _safe_float(board[0]), _safe_float(board[1]), 0.0, 0.0
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


def _safe_install(module_name: str, label: str) -> bool:
    try:
        mod = __import__(module_name, fromlist=["install"])
        fn = getattr(mod, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[SUMMARY AI STRICT BOARD REST] chained %s ok=%s version=%s", label, ok, VERSION)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] chained %s failed version=%s", label, VERSION)
        return False


def _apply_hard_board_policy() -> None:
    os.environ["ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY"] = "1"
    os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "1"
    os.environ["ENTRY_LIMIT_ALLOW_WITHOUT_BOARD"] = "0"
    os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = "0"
    os.environ["SUMMARY_AI_ALLOW_BOARD_REST_RESCUE"] = "0"
    os.environ["SUMMARY_AI_CLOSE_LIMIT_FALLBACK_ON_BOARD_MISSING"] = "0"
    os.environ["SUMMARY_AI_BOARD_MISSING_LIMIT_FALLBACK"] = "0"
    os.environ.setdefault("SUMMARY_AI_REST_BOARD_CHECK_ON_MISSING", "1")
    try:
        from trading.handlers import entry_order_builder as eob
        for name, value in (
            ("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", True),
            ("ENTRY_BOARD_MISSING_HARD_BLOCK", True),
            ("ENTRY_LIMIT_ALLOW_WITHOUT_BOARD", False),
            ("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False),
        ):
            try:
                setattr(eob, name, value)
            except Exception:
                pass
    except Exception:
        pass


def _infer_source_from_stack() -> tuple[str, str]:
    source = ""
    side = ""
    try:
        for frame in inspect.stack(context=0)[:14]:
            fn = str(getattr(frame, "function", ""))
            filename = str(getattr(frame, "filename", "")).replace("\\", "/").lower()
            loc = getattr(frame, "frame", None).f_locals if getattr(frame, "frame", None) is not None else {}
            if filename.endswith("trading/handlers/entry_order_builder.py") and fn in {"_get_board_with_retry", "build_entry_order"}:
                source = str(loc.get("source") or loc.get("entry_source") or source or "SUMMARY_AI_ORDER_BUILDER")
                side = _norm_side(loc.get("side") or loc.get("entry_side") or side)
                break
            if not side:
                side = _norm_side(loc.get("side") or loc.get("entry_side") or loc.get("ai_side"))
            if not source:
                source = str(loc.get("source") or loc.get("entry_source") or "")
    except Exception:
        pass
    return source or "", side or ""


def _fetch_rest_board(symbol: Any, *, side: str = "", source: str = "") -> Any:
    try:
        from core.startup import board_retry_patch as brp
        old = os.environ.get("ENTRY_BOARD_REST_DIRECT_ENABLED")
        os.environ["ENTRY_BOARD_REST_DIRECT_ENABLED"] = "1"
        try:
            return brp._fetch_board_rest(str(symbol), side=side, source=source or "SUMMARY_AI_REST_BOARD_CHECK")
        finally:
            if old is None:
                os.environ.pop("ENTRY_BOARD_REST_DIRECT_ENABLED", None)
            else:
                os.environ["ENTRY_BOARD_REST_DIRECT_ENABLED"] = old
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] REST board check failed symbol=%s side=%s source=%s version=%s", symbol, side, source, VERSION)
        return None


def _board_or_rest_check(board: Any, symbol: Any, *, side: str = "", source: str = "", inferred: str = "") -> Any:
    if _valid_board(board):
        return board
    if not _env_bool("SUMMARY_AI_REST_BOARD_CHECK_ON_MISSING", True):
        return board
    if not source:
        stack_source, stack_side = _infer_source_from_stack()
        source = stack_source or inferred
        side = side or stack_side
    source_u = str(source or "").strip().upper()
    if not _summary_like(source_u):
        return board
    rest = _fetch_rest_board(symbol, side=side, source=source_u or "SUMMARY_AI_REST_BOARD_CHECK")
    if _valid_board(rest):
        bid, ask, bid_qty, ask_qty = _extract_bid_ask(rest)
        logger.warning(
            "[SUMMARY AI STRICT BOARD REST] REST board verified after push-missing symbol=%s side=%s source=%s bid=%s ask=%s bid_qty=%s ask_qty=%s version=%s",
            symbol, side, source_u, bid, ask, bid_qty, ask_qty, VERSION,
        )
        return rest
    logger.warning(
        "[SUMMARY AI STRICT BOARD REST] board missing confirmed by REST symbol=%s side=%s source=%s inferred=%s hard_block=1 version=%s",
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


def _install_board_wrappers() -> bool:
    global _ORIG_GET_LATEST_BID_ASK, _ORIG_GET_BOARD_WITH_RETRY
    try:
        from trading.handlers import entry_order_builder as eob

        cur_get = getattr(eob, "get_latest_bid_ask", None)
        if callable(cur_get) and not getattr(cur_get, "_summary_ai_rest_board_check_v11", False):
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
                return _board_or_rest_check(board, symbol, side=side, source=source, inferred=inferred)

            _patched_get_latest_bid_ask._summary_ai_rest_board_check_v11 = True  # type: ignore[attr-defined]
            _patched_get_latest_bid_ask._summary_ai_rest_board_check_v10 = True  # type: ignore[attr-defined]
            _patched_get_latest_bid_ask._summary_ai_rest_board_check_v9 = True  # type: ignore[attr-defined]
            _patched_get_latest_bid_ask._original = _ORIG_GET_LATEST_BID_ASK  # type: ignore[attr-defined]
            eob.get_latest_bid_ask = _patched_get_latest_bid_ask

        cur_retry = getattr(eob, "_get_board_with_retry", None)
        if callable(cur_retry) and not getattr(cur_retry, "_summary_ai_board_retry_rest_check_v11", False):
            _ORIG_GET_BOARD_WITH_RETRY = _unwrap_original(cur_retry)

            @wraps(_ORIG_GET_BOARD_WITH_RETRY)
            def _patched_get_board_with_retry(symbol: Any, *args: Any, **kwargs: Any):
                source = str(kwargs.get("source") or "").strip().upper()
                side = _norm_side(kwargs.get("side"))
                try:
                    board = _ORIG_GET_BOARD_WITH_RETRY(symbol, *args, **kwargs)
                except TypeError:
                    board = _ORIG_GET_BOARD_WITH_RETRY(symbol)
                return _board_or_rest_check(board, symbol, side=side, source=source, inferred="_get_board_with_retry")

            _patched_get_board_with_retry._summary_ai_board_retry_rest_check_v11 = True  # type: ignore[attr-defined]
            _patched_get_board_with_retry._summary_ai_board_retry_rest_check_v10 = True  # type: ignore[attr-defined]
            _patched_get_board_with_retry._summary_ai_board_retry_rest_check_v9 = True  # type: ignore[attr-defined]
            _patched_get_board_with_retry._original = _ORIG_GET_BOARD_WITH_RETRY  # type: ignore[attr-defined]
            eob._get_board_with_retry = _patched_get_board_with_retry
        return True
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] board wrapper install failed version=%s", VERSION)
        return False


def install() -> bool:
    """Use REST only to verify actual board when PUSH rotation has no bid/ask.

    Policy remains strict:
      - PUSH board missing alone is not enough to call a symbol low-liquidity.
      - REST board with real bid/ask may be used as the board source.
      - If REST board also has no bid/ask, keep STRICT_BOARD_MISSING.
      - Never rescue with close/current/vwap price when board is unavailable.
      - Summary-AI AI_OK symbols are temporarily promoted into PUSH rotation, but still
        require a real board before ordering.
      - Summary-AI ATR shortage can be bridged only when liquidity and observed range are enough.
    """
    global _INSTALLED
    if _INSTALLED:
        _apply_hard_board_policy()
        _safe_install("core.startup.summary_ai_urgent_push_registration_patch", "urgent_push")
        _safe_install("core.startup.summary_ai_atr_liquidity_bridge_patch", "atr_liquidity_bridge")
        return True
    try:
        ranking_prefilter = _safe_install("core.startup.summary_ai_ranking_prefilter_score_fallback_patch", "ranking_prefilter_score_fallback")
        best_rank_bridge = _safe_install("core.startup.summary_ai_ranking_best_rank_bridge_patch", "best_rank_bridge")
        fast_entry = _safe_install("core.startup.ranking_summary_fast_entry_patch", "ranking_summary_fast_entry")
        board_defer = _safe_install("core.startup.summary_ai_board_missing_defer_patch", "board_missing_defer")
        urgent_push = _safe_install("core.startup.summary_ai_urgent_push_registration_patch", "urgent_push")
        atr_liq_bridge = _safe_install("core.startup.summary_ai_atr_liquidity_bridge_patch", "atr_liquidity_bridge")
        _apply_hard_board_policy()
        board_wrappers = _install_board_wrappers()
        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI STRICT BOARD REST] installed REST board check for push-rotation missing; hard block remains ranking_prefilter=%s best_rank_bridge=%s fast_entry=%s board_defer=%s urgent_push=%s atr_liq_bridge=%s board_wrappers=%s close_limit_fallback=0 version=%s",
            ranking_prefilter,
            best_rank_bridge,
            fast_entry,
            board_defer,
            urgent_push,
            atr_liq_bridge,
            board_wrappers,
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI STRICT BOARD REST] auto install failed")


__all__ = ["install", "VERSION"]
