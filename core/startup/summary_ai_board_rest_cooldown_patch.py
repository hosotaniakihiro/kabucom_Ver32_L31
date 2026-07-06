# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-SUMMARY-AI-BOARD-REST-COOLDOWN-BYPASS-STRATEGY-FIB-EV"
_INSTALLED = False
_ORIG_FETCH = None
_STRATEGY_INSTALLED = False


def _summary_source(source: Any) -> bool:
    s = str(source or "").strip().upper()
    return s in {"SUMMARY", "SUMMARY_AI", "PUSH_SUMMARY", "STOCK_SUMMARY", "SUMMARY_AI_ORDER_BUILDER"} or "SUMMARY_AI" in s


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _valid_board(board: Any) -> bool:
    if not isinstance(board, dict):
        return False
    buy1 = board.get("Buy1") if isinstance(board.get("Buy1"), dict) else {}
    sell1 = board.get("Sell1") if isinstance(board.get("Sell1"), dict) else {}

    def f(v: Any) -> float:
        try:
            if v is None or str(v).strip() == "":
                return 0.0
            return float(str(v).replace(",", ""))
        except Exception:
            return 0.0

    bid = f(board.get("bid_price") or board.get("bid") or board.get("best_bid") or board.get("BidPrice") or board.get("BestBid") or buy1.get("Price"))
    ask = f(board.get("ask_price") or board.get("ask") or board.get("best_ask") or board.get("AskPrice") or board.get("BestAsk") or sell1.get("Price"))
    return bid > 0 and ask > 0


def _install_strategy_mode() -> bool:
    global _STRATEGY_INSTALLED
    if _STRATEGY_INSTALLED:
        return True
    try:
        from core.startup.summary_ai_strategy_mode_patch import install as _install_strategy
        strategy_ok = bool(_install_strategy())
    except Exception:
        strategy_ok = False
        logger.exception("[SUMMARY AI BOARD REST COOLDOWN] chained strategy mode install failed version=%s", VERSION)
    try:
        from core.startup.summary_ai_fibonacci_ev_strategy_patch import install as _install_fib_ev
        fib_ok = bool(_install_fib_ev())
    except Exception:
        fib_ok = False
        logger.exception("[SUMMARY AI BOARD REST COOLDOWN] chained fibonacci EV overlay install failed version=%s", VERSION)
    _STRATEGY_INSTALLED = bool(strategy_ok or fib_ok)
    logger.warning("[SUMMARY AI BOARD REST COOLDOWN] chained strategy mode ok=%s fib_ev=%s version=%s", strategy_ok, fib_ok, VERSION)
    return _STRATEGY_INSTALLED


def install() -> bool:
    global _INSTALLED, _ORIG_FETCH
    strategy_ok = _install_strategy_mode()
    if _INSTALLED:
        return True
    try:
        from core.startup import board_retry_patch as brp

        cur = getattr(brp, "_fetch_board_rest", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI BOARD REST COOLDOWN] fetch unavailable version=%s strategy=%s", VERSION, strategy_ok)
            return False
        if getattr(cur, "_summary_ai_cooldown_bypass_v3", False) or getattr(cur, "_summary_ai_cooldown_bypass_v2", False) or getattr(cur, "_summary_ai_cooldown_bypass_v1", False):
            _INSTALLED = True
            return True
        _ORIG_FETCH = cur

        def _patched_fetch_board_rest(symbol: str, side: str = "", source: str = ""):
            src = str(source or "")
            is_summary = _summary_source(src)
            old_timeout = os.environ.get("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC")
            if is_summary:
                # Summary-AIは候補ごとの発注直前確認なので、直前の別銘柄NGで全候補を止めない。
                try:
                    cooldown_until = float(getattr(brp, "_REST_COOLDOWN_UNTIL", 0.0) or 0.0)
                except Exception:
                    cooldown_until = 0.0
                remaining = cooldown_until - time.time()
                if remaining > 0:
                    setattr(brp, "_REST_COOLDOWN_UNTIL", 0.0)
                    logger.warning(
                        "[SUMMARY AI BOARD REST COOLDOWN] cleared cooldown before REST symbol=%s side=%s source=%s remaining=%.1fs version=%s",
                        symbol,
                        side,
                        source,
                        remaining,
                        VERSION,
                    )
                desired_timeout = max(2.0, _env_float("SUMMARY_AI_REST_BOARD_TIMEOUT_SEC", 2.0))
                if _env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 0.0) < desired_timeout:
                    os.environ["ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC"] = str(desired_timeout)
            try:
                board = _ORIG_FETCH(symbol, side=side, source=source)
            finally:
                if is_summary:
                    # 通常の板なし/timeoutで設定されたcooldownは次候補へ持ち越さない。
                    try:
                        cooldown_until = float(getattr(brp, "_REST_COOLDOWN_UNTIL", 0.0) or 0.0)
                    except Exception:
                        cooldown_until = 0.0
                    if cooldown_until > time.time():
                        setattr(brp, "_REST_COOLDOWN_UNTIL", 0.0)
                        logger.warning(
                            "[SUMMARY AI BOARD REST COOLDOWN] cleared cooldown after REST symbol=%s side=%s source=%s version=%s",
                            symbol,
                            side,
                            source,
                            VERSION,
                        )
                    if old_timeout is None:
                        os.environ.pop("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", None)
                    else:
                        os.environ["ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC"] = old_timeout
            if is_summary:
                logger.warning(
                    "[SUMMARY AI BOARD REST COOLDOWN] REST result symbol=%s side=%s source=%s valid=%s version=%s",
                    symbol,
                    side,
                    source,
                    _valid_board(board),
                    VERSION,
                )
            return board

        _patched_fetch_board_rest._summary_ai_cooldown_bypass_v3 = True  # type: ignore[attr-defined]
        _patched_fetch_board_rest._summary_ai_cooldown_bypass_v2 = True  # type: ignore[attr-defined]
        _patched_fetch_board_rest._summary_ai_cooldown_bypass_v1 = True  # type: ignore[attr-defined]
        _patched_fetch_board_rest._original = _ORIG_FETCH  # type: ignore[attr-defined]
        brp._fetch_board_rest = _patched_fetch_board_rest
        _INSTALLED = True
        logger.warning("[SUMMARY AI BOARD REST COOLDOWN] installed version=%s strategy=%s", VERSION, strategy_ok)
        return True
    except Exception:
        logger.exception("[SUMMARY AI BOARD REST COOLDOWN] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BOARD REST COOLDOWN] auto install failed")


__all__ = ["install", "VERSION"]
