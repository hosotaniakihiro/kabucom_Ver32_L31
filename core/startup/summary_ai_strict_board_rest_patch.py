# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V8-SUMMARY-AI-BOARD-HARD-BLOCK-DEFER-RANKING-BRIDGES"
_INSTALLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


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


def install() -> bool:
    """Keep SUMMARY_AI board-missing behavior strict and chain retry/defer bridges.

    Board missing remains a hard block because it may be low liquidity.
    V8 also marks board-missing snapshots retryable/deferred for short PUSH
    rotation gaps. It never allows a boardless order.
    """
    global _INSTALLED
    if _INSTALLED:
        _apply_hard_board_policy()
        return True
    try:
        ranking_prefilter = _safe_install("core.startup.summary_ai_ranking_prefilter_score_fallback_patch", "ranking_prefilter_score_fallback")
        best_rank_bridge = _safe_install("core.startup.summary_ai_ranking_best_rank_bridge_patch", "best_rank_bridge")
        fast_entry = _safe_install("core.startup.ranking_summary_fast_entry_patch", "ranking_summary_fast_entry")
        board_defer = _safe_install("core.startup.summary_ai_board_missing_defer_patch", "board_missing_defer")
        _apply_hard_board_policy()
        if not _env_bool("SUMMARY_AI_ALLOW_BOARD_REST_RESCUE", False):
            _INSTALLED = True
            logger.warning(
                "[SUMMARY AI STRICT BOARD REST] disabled by policy; board missing remains hard block ranking_prefilter=%s best_rank_bridge=%s fast_entry=%s board_defer=%s close_limit_fallback=0 version=%s",
                ranking_prefilter,
                best_rank_bridge,
                fast_entry,
                board_defer,
                VERSION,
            )
            return True

        logger.warning(
            "[SUMMARY AI STRICT BOARD REST] rescue requested but policy keeps hard block ranking_prefilter=%s best_rank_bridge=%s fast_entry=%s board_defer=%s close_limit_fallback=0 version=%s",
            ranking_prefilter,
            best_rank_bridge,
            fast_entry,
            board_defer,
            VERSION,
        )
        _INSTALLED = True
        return True
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI STRICT BOARD REST] auto install failed")


__all__ = ["install", "VERSION"]
