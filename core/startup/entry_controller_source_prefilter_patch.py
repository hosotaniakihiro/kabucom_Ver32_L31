# ============================================================
# File   : core/startup/entry_controller_source_prefilter_patch.py
# Version: V6-BUILD-SCORED-CANDIDATES-INLINED
# ------------------------------------------------------------
# V6:
#   - entry_controller._build_scored_candidates() の pipeline_source/interval
#     事前フィルタ (SUMMARY_AI/SUMMARY/PUSH 互換正規化含む) は
#     trading/handlers/entry_controller.py 本体 (_prefilter_entries_for_pipeline) へ
#     インライン化したため、この patch はもう _build_scored_candidates を差し替えない。
#     残る役割は summary_ai_volatility_rescue_patch の companion install のみ。
#
# V5:
#   - SUMMARY_AI/SUMMARY/PUSH 互換判定は trading/handlers/entry_controller.py の
#     _entry_matches_pipeline 本体へインライン化。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSTALLED = False


def _install_summary_ai_vol_rescue() -> bool:
    try:
        # Force before importing/installing rescue so setdefault in the rescue module keeps this value.
        os.environ["SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE"] = os.getenv(
            "SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE_OPERATOR",
            "0.0002",
        )
        from core.startup import summary_ai_volatility_rescue_patch as p
        fn = getattr(p, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning(
            "[ENTRY SOURCE PREFILTER] summary_ai_volatility_rescue installed=%s min_abs_slope=%s",
            ok,
            os.getenv("SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE"),
        )
        return ok
    except Exception:
        logger.exception("[ENTRY SOURCE PREFILTER] summary_ai_volatility_rescue install failed")
        return False


def install() -> bool:
    global _INSTALLED
    vol_ok = _install_summary_ai_vol_rescue()
    _INSTALLED = True
    logger.warning("[ENTRY SOURCE PREFILTER] installed v6 (build_scored_candidates inlined) vol_rescue=%s", vol_ok)
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY SOURCE PREFILTER] auto install failed")


__all__ = ["install"]
