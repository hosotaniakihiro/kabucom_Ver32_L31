# ============================================================
# File   : core/startup/startup.py
# Version: FINAL-PRODUCTION-REV23.0-THIN-ENTRYPOINT
# ------------------------------------------------------------
# 【概要】
#   system_startup の公開入口
#
# 【設計】
#   - このファイルは起動入口だけ
#   - 実際の起動順序は startup_orchestrator.py に委譲
#   - 詳細処理は push_startup / scheduler_startup / summary_startup 等へ分離
# ============================================================

from __future__ import annotations

import logging

from core.startup.startup_orchestrator import run_system_startup

logger = logging.getLogger(__name__)


def system_startup():
    logger.info("🚀 system_startup entry REV23.0-THIN-ENTRYPOINT")
    return run_system_startup()


__all__ = [
    "system_startup",
]