# ============================================================
# File   : core/startup/entry_controller_pipeline_bucket_filter_patch.py
# Version: Ver03-BUCKET-FILTER-INLINED
# ------------------------------------------------------------
# Ver03:
#   - run_entry_pipeline / get_bucket の pipeline_source・interval事前フィルタは
#     trading/handlers/entry_controller.py の run_entry_pipeline 本体 (Ver2.8) へ
#     インライン化済みのため撤去した。
#   - summary_ai_atr_failopen_patch の companion install のみ、ここに残す。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSTALLED = False


def _install_summary_ai_atr_failopen() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_ENABLED", "1")
        os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_VOLUME", "30000")
        os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_TURNOVER", "10000000")
        os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_PRICE", "1500")
        from core.startup import summary_ai_atr_failopen_patch as p
        ok = p.install()
        logger.warning("[ENTRY PIPELINE BUCKET FILTER] summary_ai_atr_failopen_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ENTRY PIPELINE BUCKET FILTER] summary_ai_atr_failopen_patch install failed")
        return False


def install() -> bool:
    global _INSTALLED
    ok = _install_summary_ai_atr_failopen()
    _INSTALLED = True
    return bool(ok)


try:
    install()
except Exception as e:
    logger.exception("[ENTRY PIPELINE BUCKET FILTER] auto install failed err=%s", e)

__all__ = ["install"]
