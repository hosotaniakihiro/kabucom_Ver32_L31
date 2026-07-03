# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-BOARD-ROTATION-FULL-CYCLE-RETRY"
_INSTALLED = False


def _safe_float(v: Any, default: float) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def install() -> bool:
    """Retry board acquisition for one full A/B PUSH rotation.

    Current PUSH rotation is roughly:
      A 4.8s -> clear 0.2s -> B 4.8s -> clear 0.2s = about 10s.
    A 5s retry can still miss the opposite side. Use 10.5s while preserving
    hard-block semantics: if board is still missing, do not place an order.
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        retry_sec = _safe_float(os.getenv("SUMMARY_AI_BOARD_FULL_ROTATION_RETRY_SEC"), 10.5)
        interval_sec = _safe_float(os.getenv("SUMMARY_AI_BOARD_FULL_ROTATION_RETRY_INTERVAL_SEC"), 0.2)
        retry_sec = max(5.0, retry_sec)
        interval_sec = max(0.1, min(interval_sec, 1.0))

        os.environ["ENTRY_ORDER_BOARD_RETRY_SEC"] = str(retry_sec)
        os.environ["ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"] = str(interval_sec)
        os.environ["SUMMARY_AI_BOARD_RETRY_REASON"] = "push_rotation_full_cycle_wait"
        os.environ["ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY"] = "1"
        os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "1"
        os.environ["ENTRY_LIMIT_ALLOW_WITHOUT_BOARD"] = "0"

        try:
            from trading.handlers import entry_order_builder as eob
            old_retry = getattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", None)
            old_interval = getattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", None)
            try:
                setattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", retry_sec)
            except Exception:
                pass
            try:
                setattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", interval_sec)
            except Exception:
                pass
            try:
                setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", True)
            except Exception:
                pass
            logger.warning(
                "[SUMMARY AI BOARD ROTATION FULL CYCLE] applied retry_sec %s->%s interval %s->%s hard_block=True version=%s",
                old_retry,
                retry_sec,
                old_interval,
                interval_sec,
                VERSION,
            )
        except Exception:
            logger.exception("[SUMMARY AI BOARD ROTATION FULL CYCLE] eob patch failed version=%s", VERSION)

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI BOARD ROTATION FULL CYCLE] installed retry_sec=%s interval=%s hard_block=True version=%s",
            retry_sec,
            interval_sec,
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI BOARD ROTATION FULL CYCLE] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BOARD ROTATION FULL CYCLE] auto install failed version=%s", VERSION)


__all__ = ["install", "VERSION"]
