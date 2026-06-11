from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def install() -> bool:
    """Legacy rescue shim.

    This patch used to fail-open Tonosama when recent 3m/5m or surge history was
    missing.  That made startup noisy and could allow weak/low-information
    entries.  Keep it available as an explicit escape hatch, but do not alter the
    core Tonosama decision path by default.
    """
    try:
        if not _env_on("USERCUSTOMIZE_ENABLE_LEGACY_TONOSAMA_FAILOPEN_PATCHES", False):
            logger.warning(
                "[TONOSAMA RECENT 3M5M FAILOPEN] skipped; legacy fail-open disabled. "
                "Set USERCUSTOMIZE_ENABLE_LEGACY_TONOSAMA_FAILOPEN_PATCHES=1 to restore."
            )
            return True

        os.environ.setdefault("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", "1")
        os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", "1")
        os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", "3.0")
        os.environ.setdefault("TONOSAMA_AI_FALLBACK_ZERO_SURGE_RESCUE", "1")
        os.environ.setdefault("TONOSAMA_AI_FALLBACK_PRICE_RANGE_RESCUE", "1")
        os.environ.setdefault("TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC", "0")
        logger.warning(
            "[TONOSAMA RECENT 3M5M FAILOPEN] installed legacy allow_without_history=%s failopen=%s value=%s",
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
