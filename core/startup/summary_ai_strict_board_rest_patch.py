# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V6-SUMMARY-AI-BOARD-MISSING-HARD-BLOCK-NO-LIMIT-FALLBACK"
_INSTALLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _install_ranking_prefilter_score_fallback() -> bool:
    try:
        from core.startup.summary_ai_ranking_prefilter_score_fallback_patch import install as _install_ranking_prefilter
        return bool(_install_ranking_prefilter())
    except Exception:
        logger.exception("[SUMMARY AI STRICT BOARD REST] ranking prefilter fallback chain install failed version=%s", VERSION)
        return False


def _apply_hard_board_policy() -> None:
    """Board missing must remain an entry block.

    This also disables older V3.6 fast-order-builder close/LIMIT fallback by env,
    so even if an old wrapper is still imported locally it cannot convert
    STRICT_BOARD_MISSING into an order.
    """
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
    """Keep SUMMARY_AI board-missing behavior strict by default.

    User policy:
      If board data cannot be obtained, the symbol is likely low-liquidity or
      unsafe to enter. Do not rescue it with REST/close/LIMIT fallback.

    V6:
      - Force disables SUMMARY_AI_CLOSE_LIMIT_FALLBACK_ON_BOARD_MISSING so older
        V3.6 fast-order wrappers cannot convert STRICT_BOARD_MISSING into LIMIT.
      - Keeps the RANKING prefilter score bridge installed.
    """
    global _INSTALLED
    if _INSTALLED:
        _apply_hard_board_policy()
        return True
    try:
        ranking_prefilter = _install_ranking_prefilter_score_fallback()
        _apply_hard_board_policy()
        if not _env_bool("SUMMARY_AI_ALLOW_BOARD_REST_RESCUE", False):
            _INSTALLED = True
            logger.warning(
                "[SUMMARY AI STRICT BOARD REST] disabled by policy; board missing remains hard block ranking_prefilter=%s close_limit_fallback=0 version=%s",
                ranking_prefilter,
                VERSION,
            )
            return True

        # Debug-only escape hatch is intentionally ignored. Board-missing entries are unsafe.
        logger.warning(
            "[SUMMARY AI STRICT BOARD REST] rescue requested but policy keeps hard block ranking_prefilter=%s close_limit_fallback=0 version=%s",
            ranking_prefilter,
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
