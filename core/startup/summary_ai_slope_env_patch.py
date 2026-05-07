# ============================================================
# File   : core/startup/summary_ai_slope_env_patch.py
# Version: PRODUCTION-STABLE-REV1-SUMMARY-AI-SLOPE-ENV-PATCH
# ------------------------------------------------------------
# Purpose:
#   - trading.entry.summary_ai.candidates は slope gate を環境変数から読む。
#   - 現在 1m SUMMARY が slope 条件で candidates=0 になっているため、
#     未設定時だけ slope 条件を緩める。
#   - 明示的に環境変数が設定されている場合は上書きしない。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSTALLED = False

_DEFAULTS = {
    "ENTRY_MIN_BUY_SLOPE": "-999",
    "SUMMARY_AI_MIN_BUY_SLOPE": "-999",
    "ENTRY_MAX_SELL_SLOPE": "999",
    "SUMMARY_AI_MAX_SELL_SLOPE": "999",
}


def _blank(v: object) -> bool:
    try:
        return v is None or str(v).strip() == ""
    except Exception:
        return True


def install_summary_ai_slope_env_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    applied: dict[str, str] = {}
    kept: dict[str, str] = {}

    for key, value in _DEFAULTS.items():
        cur = os.environ.get(key)
        if _blank(cur):
            os.environ[key] = value
            applied[key] = value
        else:
            kept[key] = str(cur)

    _INSTALLED = True
    logger.warning("[SUMMARY AI SLOPE ENV PATCH] installed applied=%s kept=%s", applied, kept)


__all__ = ["install_summary_ai_slope_env_patch"]
