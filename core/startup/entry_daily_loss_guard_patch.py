from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def install() -> bool:
    logger.warning("[ENTRY DAILY GUARD] disabled no-op by request")
    return True


def daily_stats() -> dict:
    return {"ok": True, "disabled": True, "realized_pnl": 0.0, "exit_count": 0, "consecutive_losses": 0}


def should_block_entry() -> tuple[bool, str, dict]:
    return False, "DISABLED_BY_REQUEST", daily_stats()


__all__ = ["install", "daily_stats", "should_block_entry"]
