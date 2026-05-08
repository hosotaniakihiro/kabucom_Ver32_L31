# ============================================================
# File   : core/startup/__init__.py
# Ver    : PRODUCTION-STABLE-REV20.7-STARTUP-PACKAGE-ALLOW-ORDERS-PATCH
# ------------------------------------------------------------
# 【概要】
#   core.startup パッケージの公開入口
#   schedule loop stale guard patch / summary scheduler timeout patch /
#   summary AI slope env patch / summary AI score env patch /
#   summary write gate runtime patch / ranking summary persistence lock patch /
#   allow orders runtime patch を自動適用する。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from .schedule_loop_stale_patch import install_schedule_loop_stale_patch

    install_schedule_loop_stale_patch()
except Exception:
    logger.exception("[core.startup] schedule loop stale patch install failed")

try:
    from .summary_scheduler_timeout_patch import install_summary_scheduler_timeout_patch

    install_summary_scheduler_timeout_patch()
except Exception:
    logger.exception("[core.startup] summary scheduler timeout patch install failed")

try:
    from .summary_ai_slope_env_patch import install_summary_ai_slope_env_patch

    install_summary_ai_slope_env_patch()
except Exception:
    logger.exception("[core.startup] summary AI slope env patch install failed")

try:
    from .summary_ai_score_env_patch import install_summary_ai_score_env_patch

    install_summary_ai_score_env_patch()
except Exception:
    logger.exception("[core.startup] summary AI score env patch install failed")

try:
    from .summary_write_gate_runtime_patch import install_summary_write_gate_runtime_patch

    install_summary_write_gate_runtime_patch()
except Exception:
    logger.exception("[core.startup] summary write gate runtime patch install failed")

try:
    from .ranking_summary_persistence_lock_patch import install_ranking_summary_persistence_lock_patch

    install_ranking_summary_persistence_lock_patch()
except Exception:
    logger.exception("[core.startup] ranking summary persistence lock patch install failed")

try:
    from .allow_orders_runtime_patch import install_allow_orders_runtime_patch

    install_allow_orders_runtime_patch()
except Exception:
    logger.exception("[core.startup] allow orders runtime patch install failed")

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
