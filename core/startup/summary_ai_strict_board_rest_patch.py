# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V5-SUMMARY-AI-BOARD-MISSING-HARD-BLOCK-RANKING-PREFILTER"
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


def install() -> bool:
    """Keep SUMMARY_AI board-missing behavior strict by default.

    User policy:
      If board data cannot be obtained, the symbol is likely low-liquidity or
      unsafe to enter. Do not rescue it with REST fallback. A dedicated fast
      order builder may convert a Summary-AI STRICT_BOARD_MISSING into a safe
      LIMIT fallback after all normal checks have passed.

    V5 also installs the RANKING prefilter score bridge so RANKING rows are not
    all removed when ranking_score/ranking_momentum are zero but existing
    score_buy/score_sell/score_mtf/score_slope are populated.
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        ranking_prefilter = _install_ranking_prefilter_score_fallback()
        if not _env_bool("SUMMARY_AI_ALLOW_BOARD_REST_RESCUE", False):
            # Make the strict policy explicit. entry_order_builder will return
            # STRICT_BOARD_MISSING when the board is unavailable.
            os.environ["ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY"] = "1"
            os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "1"
            os.environ["ENTRY_LIMIT_ALLOW_WITHOUT_BOARD"] = "0"
            try:
                from trading.handlers import entry_order_builder as eob
                try:
                    setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", True)
                except Exception:
                    pass
            except Exception:
                pass
            _INSTALLED = True
            logger.warning(
                "[SUMMARY AI STRICT BOARD REST] disabled by policy; board missing remains hard block ranking_prefilter=%s version=%s",
                ranking_prefilter,
                VERSION,
            )
            return True

        # Debug-only escape hatch. Kept off by default because board-missing
        # entries are now considered unsafe/low-liquidity.
        logger.warning(
            "[SUMMARY AI STRICT BOARD REST] rescue requested by env but disabled in V5 policy ranking_prefilter=%s version=%s",
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
