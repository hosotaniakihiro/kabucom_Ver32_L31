# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-SUMMARY-AI-STRICT-REST-BOARD-FALLBACK-RANKING-PREFILTER"
_INSTALLED = False
_ORIG_GET_LATEST_BID_ASK = None


def _valid_board(board: Any) -> bool:
    try:
        if not isinstance(board, dict):
            return False
        bid = float(board.get("bid_price") or board.get("bid") or board.get("best_bid") or 0)
        ask = float(board.get("ask_price") or board.get("ask") or board.get("best_ask") or 0)
        return bid > 0 and ask > 0
    except Exception:
        return False


def _summary_like(source: str) -> bool:
    s = str(source or "").strip().upper()
    return s in {"SUMMARY_AI", "SUMMARY", "PUSH_SUMMARY", "STOCK_SUMMARY", "PUSH", "SUMMARY_AI_ORDER_BUILDER"}


def _infer_source_from_stack() -> str:
    """entry_order_builder._get_board_with_retry calls get_latest_bid_ask(symbol)
    without passing source/side, so infer SUMMARY_AI when the call comes from
    the order-builder retry path.
    """
    try:
        for frame in inspect.stack(context=0)[:8]:
            fn = str(getattr(frame, "function", ""))
            filename = str(getattr(frame, "filename", "")).replace("\\", "/").lower()
            if fn == "_get_board_with_retry" and filename.endswith("trading/handlers/entry_order_builder.py"):
                return "SUMMARY_AI_ORDER_BUILDER"
    except Exception:
        pass
    return ""


def _install_ranking_prefilter_score_fallback() -> bool:
    try:
        from core.startup.summary_ai_ranking_prefilter_score_fallback_patch import install as _install_ranking_prefilter
        return bool(_install_ranking_prefilter())
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] ranking prefilter fallback chain install failed version=%s", VERSION)
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_GET_LATEST_BID_ASK
    if _INSTALLED:
        return True
    try:
        ranking_prefilter = _install_ranking_prefilter_score_fallback()
        from trading.handlers import entry_order_builder as eob
        cur = getattr(eob, "get_latest_bid_ask", None)
        if not callable(cur):
            return ranking_prefilter
        if getattr(cur, "_summary_ai_strict_board_rest_v3", False):
            _INSTALLED = True
            return True
        _ORIG_GET_LATEST_BID_ASK = getattr(cur, "_original", cur)

        def _patched_get_latest_bid_ask(symbol: Any, *args: Any, **kwargs: Any):
            source = str(kwargs.get("source") or "").strip().upper()
            side = str(kwargs.get("side") or "").strip().upper()
            inferred = ""
            try:
                board = _ORIG_GET_LATEST_BID_ASK(symbol)
            except TypeError:
                board = _ORIG_GET_LATEST_BID_ASK(symbol, *args, **kwargs)
            if _valid_board(board):
                return board
            if not source:
                inferred = _infer_source_from_stack()
                source = inferred
            if not _summary_like(source):
                return board
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
                if _valid_board(rest):
                    logger.warning(
                        "[SUMMARY AI STRICT BOARD REST] board recovered symbol=%s side=%s source=%s inferred=%s version=%s",
                        symbol, side, source, inferred, VERSION,
                    )
                    return rest
                logger.warning(
                    "[SUMMARY AI STRICT BOARD REST] board still missing symbol=%s side=%s source=%s inferred=%s ranking_prefilter=%s version=%s",
                    symbol, side, source, inferred, ranking_prefilter, VERSION,
                )
                return board
            except Exception:
                logger.exception("[SUMMARY AI STRICT BOARD REST] failed symbol=%s side=%s source=%s inferred=%s version=%s", symbol, side, source, inferred, VERSION)
                return board

        _patched_get_latest_bid_ask._summary_ai_strict_board_rest_v1 = True  # type: ignore[attr-defined]
        _patched_get_latest_bid_ask._summary_ai_strict_board_rest_v2 = True  # type: ignore[attr-defined]
        _patched_get_latest_bid_ask._summary_ai_strict_board_rest_v3 = True  # type: ignore[attr-defined]
        _patched_get_latest_bid_ask._original = _ORIG_GET_LATEST_BID_ASK  # type: ignore[attr-defined]
        eob.get_latest_bid_ask = _patched_get_latest_bid_ask
        _INSTALLED = True
        logger.warning("[SUMMARY AI STRICT BOARD REST] installed version=%s ranking_prefilter=%s", VERSION, ranking_prefilter)
        return True
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI STRICT BOARD REST] auto install failed")


__all__ = ["install", "VERSION"]
