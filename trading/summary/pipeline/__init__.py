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

# indicator_short_history_patch は indicator_pipeline.run_indicator_pipeline 本体へ
# 本文化した (計算本体は indicator_short_history.py)。install は不要。

from .summary_pipeline import run_summary_pipeline

__all__ = ["run_summary_pipeline"]
