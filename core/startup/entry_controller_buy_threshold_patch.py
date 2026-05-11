# ============================================================
# File   : core/startup/entry_controller_buy_threshold_patch.py
# Version: PRODUCTION-ENTRY-BUY-THRESHOLD-PATCH-V1
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI の BUY候補は score_config 上 4.0 点でAI_OKになるが、
#   entry_controller.py 側の最終BUY閾値が 5.0 のままだと
#   BUY_SCORE_LOW / BUY_COMPOSITE_LOW で最終発注前に落ちる。
#
# 方針:
#   起動時に entry_controller の BUY 最終閾値を実運用スコアに合わせる。
#   SELL 側の閾値は変更しない。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PATCHED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def install() -> bool:
    global _PATCHED

    if _PATCHED:
        return True

    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY BUY THRESHOLD PATCH] import failed")
        return False

    try:
        min_buy_score = _env_float("ENTRY_CONTROLLER_MIN_SUMMARY_SCORE_BUY", 3.0)
        min_buy_composite = _env_float("ENTRY_CONTROLLER_MIN_COMPOSITE_SCORE_BUY", 3.0)
        min_buy_conf = _env_float("ENTRY_CONTROLLER_MIN_AI_CONFIDENCE_BUY", 0.60)

        old_score = getattr(ec, "MIN_SUMMARY_SCORE_BUY", None)
        old_comp = getattr(ec, "MIN_COMPOSITE_SCORE_BUY", None)
        old_conf = getattr(ec, "MIN_AI_CONFIDENCE_BUY", None)

        ec.MIN_SUMMARY_SCORE_BUY = float(min_buy_score)
        ec.MIN_COMPOSITE_SCORE_BUY = float(min_buy_composite)
        ec.MIN_AI_CONFIDENCE_BUY = float(min_buy_conf)

        _PATCHED = True

        logger.warning(
            "[ENTRY BUY THRESHOLD PATCH] installed MIN_SUMMARY_SCORE_BUY %s->%s MIN_COMPOSITE_SCORE_BUY %s->%s MIN_AI_CONFIDENCE_BUY %s->%s",
            old_score,
            ec.MIN_SUMMARY_SCORE_BUY,
            old_comp,
            ec.MIN_COMPOSITE_SCORE_BUY,
            old_conf,
            ec.MIN_AI_CONFIDENCE_BUY,
        )
        return True

    except Exception:
        logger.exception("[ENTRY BUY THRESHOLD PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY BUY THRESHOLD PATCH] auto install failed")
