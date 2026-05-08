# ============================================================
# File   : trading/summary/pipeline/__init__.py
# Version: PRODUCTION-STABLE-PIPELINE-INIT-V1-SHORT-HISTORY-PATCH
# ------------------------------------------------------------
# Purpose:
#   summary pipeline package 初期化。
#   indicator_pipeline の短履歴フォールバックパッチを自動適用する。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from .indicator_short_history_patch import install_indicator_short_history_patch

    install_indicator_short_history_patch()
except Exception:
    logger.exception("[summary.pipeline] indicator short history patch install failed")

from .summary_pipeline import run_summary_pipeline

__all__ = ["run_summary_pipeline"]
