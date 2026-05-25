# ============================================================
# File   : core/startup/__init__.py
# Ver    : PRODUCTION-STABLE-REV21.3-SOFT-TECHNICAL-READY
# ------------------------------------------------------------
# 【概要】
#   core.startup パッケージの公開入口。
#   各種 runtime/startup patch を自動適用する。
#
# REV21.3:
#   - summary_controller_soft_technical_ready_patch を追加
#   - 短履歴PUSHサマリーで指標があるのに technical_ready=False 固定になる問題を補正
#   - summary_runner_ready_fill_before_save_patch も継続適用
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
    from .summary_scheduler_unified_stale_guard_patch import install as install_summary_scheduler_unified_stale_guard_patch

    install_summary_scheduler_unified_stale_guard_patch()
except Exception:
    logger.exception("[core.startup] summary scheduler unified stale guard patch install failed")

try:
    from .summary_controller_soft_technical_ready_patch import install as install_summary_controller_soft_technical_ready_patch

    install_summary_controller_soft_technical_ready_patch()
except Exception:
    logger.exception("[core.startup] summary controller soft technical ready patch install failed")

try:
    from .summary_runner_ready_fill_before_save_patch import install as install_summary_runner_ready_fill_before_save_patch

    install_summary_runner_ready_fill_before_save_patch()
except Exception:
    logger.exception("[core.startup] summary runner ready fill before save patch install failed")

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

try:
    from .exit_limit_board_touch_runtime_patch import install as install_exit_limit_board_touch_patch

    install_exit_limit_board_touch_patch()
except Exception:
    logger.exception("[core.startup] exit board touch limit patch install failed")

try:
    from .summary_existing_null_repair_patch import install as install_summary_existing_null_repair_patch

    install_summary_existing_null_repair_patch()
except Exception:
    logger.exception("[core.startup] summary existing null repair patch install failed")

try:
    from .daily_signal_cache_fast_startup_patch import install as install_daily_signal_cache_fast_startup_patch

    install_daily_signal_cache_fast_startup_patch()
except Exception:
    logger.exception("[core.startup] daily signal cache fast startup patch install failed")

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
