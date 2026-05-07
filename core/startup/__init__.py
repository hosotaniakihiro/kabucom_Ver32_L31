# ============================================================
# File   : core/startup/__init__.py
# Ver    : PRODUCTION-STABLE-REV20.1-STARTUP-PACKAGE-STALE-GUARD
# ------------------------------------------------------------
# 【概要】
#   core.startup パッケージの公開入口
#   schedule loop stale guard patch を自動適用する。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from .schedule_loop_stale_patch import install_schedule_loop_stale_patch

    install_schedule_loop_stale_patch()
except Exception:
    logger.exception("[core.startup] schedule loop stale patch install failed")

from .startup import system_startup
from .summary_bootstrap import (
    bootstrap_summary,
    run_bootstrap_incremental_rebuild_if_available,
)

__all__ = [
    "system_startup",
    "bootstrap_summary",
    "run_bootstrap_incremental_rebuild_if_available",
]
