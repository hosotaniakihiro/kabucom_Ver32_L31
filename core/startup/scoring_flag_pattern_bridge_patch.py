# ============================================================
# File   : core/startup/scoring_flag_pattern_bridge_patch.py
# Version: Ver01-INSTALL-FLAGS-PATTERNS-SCORE-BRIDGE
# ------------------------------------------------------------
# trading/scoring/flags と trading/scoring/patterns をスコアへ反映する
# runtime patch。
#
# 目的:
#   - 既存の scoring_core / detail_score_builder の本体を壊さずに接続する
#   - build_detail_scores() の戻り値に対して
#     flag_pattern_score_bridge.attach_flag_and_pattern_scores() を適用する
#   - flags 配下、patterns 配下の結果を score_base / direction_penalty へ反映
#
# ログ:
#   [SCORING FLAG/PATTERN PATCH] installed
#   [FLAG/PATTERN SCORE BRIDGE] attached ...
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_BUILD_DETAIL_SCORES = None


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD_DETAIL_SCORES
    if _INSTALLED:
        return True
    try:
        import trading.scoring.core.detail_score_builder as dsb
        from trading.scoring.core.flag_pattern_score_bridge import attach_flag_and_pattern_scores

        old = getattr(dsb, "build_detail_scores", None)
        if not callable(old):
            logger.warning("[SCORING FLAG/PATTERN PATCH] build_detail_scores not callable")
            return False

        if getattr(old, "_flag_pattern_bridge_patched", False):
            _INSTALLED = True
            return True

        _ORIG_BUILD_DETAIL_SCORES = old

        def _patched_build_detail_scores(df: Any, *args, **kwargs):
            out = old(df, *args, **kwargs)
            try:
                out = attach_flag_and_pattern_scores(out)
            except Exception:
                logger.exception("[SCORING FLAG/PATTERN PATCH] attach bridge failed")
            return out

        _patched_build_detail_scores._flag_pattern_bridge_patched = True  # type: ignore[attr-defined]
        dsb.build_detail_scores = _patched_build_detail_scores

        _INSTALLED = True
        logger.warning("[SCORING FLAG/PATTERN PATCH] installed")
        return True
    except Exception:
        logger.exception("[SCORING FLAG/PATTERN PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SCORING FLAG/PATTERN PATCH] auto install failed")

__all__ = ["install"]
