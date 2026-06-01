from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def install() -> bool:
    try:
        os.environ.setdefault("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", "1")
        os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", "1")
        os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", "3.0")
        os.environ.setdefault("TONOSAMA_AI_FALLBACK_ZERO_SURGE_RESCUE", "1")
        os.environ.setdefault("TONOSAMA_AI_FALLBACK_PRICE_RANGE_RESCUE", "1")
        os.environ.setdefault("TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC", "0")
        logger.warning(
            "[TONOSAMA RECENT 3M5M FAILOPEN] installed allow_without_history=%s failopen=%s value=%s",
            os.environ.get("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY"),
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA RECENT 3M5M FAILOPEN] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[TONOSAMA RECENT 3M5M FAILOPEN] auto install failed")

__all__ = ["install"]
