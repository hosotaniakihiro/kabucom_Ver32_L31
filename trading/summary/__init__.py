# ============================================================
# File   : trading/summary/__init__.py
# Version: PRODUCTION-STABLE-SUMMARY-INIT-V1-SAFE-CACHE-PATCH
# ------------------------------------------------------------
# Purpose:
#   trading.summary package 初期化。
#   summary_controller の merged cache 上書き事故対策パッチを自動適用する。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from .controller_cache_safe_set_patch import install_controller_cache_safe_set_patch

    install_controller_cache_safe_set_patch()
except Exception:
    logger.exception("[trading.summary] controller cache safe set patch install failed")


__all__ = []
