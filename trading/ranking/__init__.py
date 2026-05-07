# ============================================================
# File   : trading/ranking/__init__.py
# Version: PRODUCTION-STABLE-RANKING-INIT-V1-LOCK-PATCH
# ------------------------------------------------------------
# Purpose:
#   trading.ranking package 初期化。
#   ranking DB writer の SQLite locked 対策パッチを自動適用する。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from .ranking_db_writer_lock_patch import install_ranking_db_writer_lock_patch

    install_ranking_db_writer_lock_patch()
except Exception:
    logger.exception("[trading.ranking] ranking DB writer lock patch install failed")


__all__ = []
