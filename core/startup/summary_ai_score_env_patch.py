# ============================================================
# File   : core/startup/summary_ai_score_env_patch.py
# Version: PRODUCTION-STABLE-REV1-SUMMARY-AI-SCORE-ENV-PATCH
# ------------------------------------------------------------
# Purpose:
#   - AI.entry_gate の SUMMARY score threshold は MIN_ENTRY_SCORE を参照する。
#   - BUY候補は runner/candidates 側で min_buy=4.0 に絞られているため、
#     MIN_ENTRY_SCORE を 3.0 にしても BUY が3点台で通ることは通常ない。
#   - SELL候補の score_low:<4.000 を避けるため、未設定時だけ 3.0 にする。
#
# Expected:
#   - score_low:3.xxx<4.000 が減る
#   - 次の停止理由が low_turnover / dominant_low / executor 側に進む
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSTALLED = False


def _blank(v: object) -> bool:
    try:
        return v is None or str(v).strip() == ""
    except Exception:
        return True


def install_summary_ai_score_env_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    applied: dict[str, str] = {}
    kept: dict[str, str] = {}

    key = "MIN_ENTRY_SCORE"
    cur = os.environ.get(key)
    if _blank(cur):
        os.environ[key] = "3.0"
        applied[key] = "3.0"
    else:
        kept[key] = str(cur)

    _INSTALLED = True
    logger.warning("[SUMMARY AI SCORE ENV PATCH] installed applied=%s kept=%s", applied, kept)


__all__ = ["install_summary_ai_score_env_patch"]
