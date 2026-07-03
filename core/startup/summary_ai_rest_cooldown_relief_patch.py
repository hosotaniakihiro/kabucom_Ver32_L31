# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-REST-COOLDOWN-RELIEF"
_INSTALLED = False


def install() -> bool:
    """Shorten REST board cooldown used by Summary-AI board confirmation.

    Boardless entry is still forbidden. This only prevents one temporary REST
    board error from blocking all subsequent Summary-AI board checks for 60s.
    """
    global _INSTALLED
    try:
        old_timeout = os.environ.get("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC")
        old_cooldown = os.environ.get("ENTRY_BOARD_REST_ERROR_COOLDOWN_SEC")
        os.environ["ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC"] = os.getenv("SUMMARY_AI_REST_BOARD_TIMEOUT_SEC", "1.5")
        os.environ["ENTRY_BOARD_REST_ERROR_COOLDOWN_SEC"] = os.getenv("SUMMARY_AI_REST_BOARD_ERROR_COOLDOWN_SEC", "10")
        os.environ.setdefault("ENTRY_BOARD_REST_CACHE_TTL_SEC", "1.0")
        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI REST COOLDOWN RELIEF] installed timeout %s->%s cooldown %s->%s cache_ttl=%s hard_block=True version=%s",
            old_timeout,
            os.environ.get("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC"),
            old_cooldown,
            os.environ.get("ENTRY_BOARD_REST_ERROR_COOLDOWN_SEC"),
            os.environ.get("ENTRY_BOARD_REST_CACHE_TTL_SEC"),
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI REST COOLDOWN RELIEF] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI REST COOLDOWN RELIEF] auto install failed")


__all__ = ["install", "VERSION"]
