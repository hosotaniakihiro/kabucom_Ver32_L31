# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V4-SUMMARY-AI-BOARD-MISSING-HARD-BLOCK"
_INSTALLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def install() -> bool:
    """Keep SUMMARY_AI board-missing behavior strict by default.

    User policy:
      If board data cannot be obtained, the symbol is likely low-liquidity or
      unsafe to enter. Do not rescue it with REST fallback or close-price fallback.

    This module used to wrap entry_order_builder.get_latest_bid_ask and
    _get_board_with_retry to perform REST fallback. V4 intentionally does not
    install those wrappers unless explicitly re-enabled by env for debugging.
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
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
                "[SUMMARY AI STRICT BOARD REST] disabled by policy; board missing remains hard block version=%s",
                VERSION,
            )
            return True

        # Debug-only escape hatch. Kept off by default because board-missing
        # entries are now considered unsafe/low-liquidity.
        logger.warning(
            "[SUMMARY AI STRICT BOARD REST] rescue requested by env but disabled in V4 policy version=%s",
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
