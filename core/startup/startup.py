# ============================================================
# File   : core/startup/startup.py
# Version: FINAL-PRODUCTION-REV25.0-SUMMARY-PENDING-DUP-RESCUE
# ------------------------------------------------------------
# 【概要】
#   system_startup の公開入口
#
# 【設計】
#   - このファイルは起動入口だけ
#   - 実際の起動順序は startup_orchestrator.py に委譲
#   - 詳細処理は push_startup / scheduler_startup / summary_startup 等へ分離
#
# REV25.0:
#   - summary_entry_pending_duplicate_registered_patch を起動時に明示適用
#   - SUMMARY AI direct dispatch が duplicate existing pending を no_pending_registered と誤判定して
#     発注前に止まる問題を抑止
# ============================================================

from __future__ import annotations

import logging

from core.startup.startup_orchestrator import run_system_startup

logger = logging.getLogger(__name__)


def _install_entrypoint_runtime_patches() -> None:
    try:
        from core.startup.board_settings_env_bridge_patch import install as install_board_settings_env_bridge

        install_board_settings_env_bridge()
    except Exception:
        logger.exception("[startup.entrypoint] board settings env bridge install failed")

    try:
        from core.startup.board_runtime_safety_clamp_patch import install as install_board_runtime_safety_clamp

        install_board_runtime_safety_clamp()
    except Exception:
        logger.exception("[startup.entrypoint] board runtime safety clamp install failed")

    try:
        from core.startup.board_runtime_diagnostics_patch import install as install_board_runtime_diagnostics

        install_board_runtime_diagnostics()
    except Exception:
        logger.exception("[startup.entrypoint] board runtime diagnostics install failed")

    try:
        from core.startup.board_rest_api_monitor_patch import install as install_board_rest_api_monitor

        install_board_rest_api_monitor()
    except Exception:
        logger.exception("[startup.entrypoint] board REST API monitor install failed")

    try:
        from core.startup.tonosama_history_missing_guard_patch import install as install_tonosama_history_guard

        install_tonosama_history_guard()
    except Exception:
        logger.exception("[startup.entrypoint] tonosama history missing guard install failed")

    try:
        from core.startup.summary_seed_recent_merged_guard_patch import install as install_summary_seed_recent_guard

        install_summary_seed_recent_guard()
    except Exception:
        logger.exception("[startup.entrypoint] summary seed recent merged guard install failed")

    try:
        from core.startup.daily_mtf_daily_src_alias_patch import install as install_daily_mtf_src_alias_patch

        install_daily_mtf_src_alias_patch()
    except Exception:
        logger.exception("[startup.entrypoint] daily mtf src alias patch install failed")

    try:
        from core.startup.summary_controller_publish_mtf_merged_patch import install as install_publish_mtf_merged_patch

        install_publish_mtf_merged_patch()
    except Exception:
        logger.exception("[startup.entrypoint] publish mtf merged patch install failed")

    try:
        from core.startup.summary_push_bg_due_interval_guard_patch import install as install_push_bg_due_guard

        install_push_bg_due_guard()
    except Exception:
        logger.exception("[startup.entrypoint] push bg due interval guard install failed")

    try:
        from core.startup.entry_pipeline_pending_root_prefilter_patch import install as install_entry_pending_root_prefilter

        install_entry_pending_root_prefilter()
    except Exception:
        logger.exception("[startup.entrypoint] entry pending root prefilter install failed")

    try:
        from core.startup.entry_volume_direction_guard_patch import install as install_entry_volume_direction_guard

        install_entry_volume_direction_guard()
    except Exception:
        logger.exception("[startup.entrypoint] entry volume direction guard install failed")

    try:
        from core.startup.ranking_entry_fast_runtime_patch import install as install_ranking_entry_fast_patch

        install_ranking_entry_fast_patch()
    except Exception:
        logger.exception("[startup.entrypoint] ranking entry fast patch install failed")

    try:
        from core.startup.tonosama_fast_score_prefilter_patch import install as install_tonosama_fast_score_prefilter

        install_tonosama_fast_score_prefilter()
    except Exception:
        logger.exception("[startup.entrypoint] tonosama fast score prefilter install failed")

    try:
        from core.startup.summary_entry_pending_duplicate_registered_patch import install as install_summary_entry_pending_duplicate_registered_patch

        install_summary_entry_pending_duplicate_registered_patch()
    except Exception:
        logger.exception("[startup.entrypoint] summary entry duplicate pending registered patch install failed")

    try:
        from core.startup.rest_full_board_entry_patch import install as install_rest_full_board_entry_patch

        install_rest_full_board_entry_patch()
    except Exception:
        logger.exception("[startup.entrypoint] REST full board entry patch install failed")

    try:
        from core.startup.final_entry_safety_guard_patch import install as install_final_entry_safety_guard_patch

        install_final_entry_safety_guard_patch()
    except Exception:
        logger.exception("[startup.entrypoint] final entry safety guard install failed")

    try:
        from core.startup.exit_limit_pending_close_runtime_patch import install as install_exit_limit_pending_close_patch

        install_exit_limit_pending_close_patch()
    except Exception:
        logger.exception("[startup.entrypoint] exit limit pending close patch install failed")

    try:
        from core.startup.exit_unfilled_reprice_runtime_patch import install as install_exit_unfilled_reprice_patch

        install_exit_unfilled_reprice_patch()
    except Exception:
        logger.exception("[startup.entrypoint] exit unfilled reprice patch install failed")

    try:
        from core.startup.exit_order_fill_confirm_runtime_patch import install as install_exit_order_fill_confirm_patch

        install_exit_order_fill_confirm_patch()
    except Exception:
        logger.exception("[startup.entrypoint] exit order fill confirm patch install failed")

    try:
        from core.startup.exit_closing_stale_reconcile_runtime_patch import install as install_exit_closing_stale_reconcile_patch

        install_exit_closing_stale_reconcile_patch()
    except Exception:
        logger.exception("[startup.entrypoint] exit closing stale reconcile patch install failed")

    try:
        from core.startup.board_runtime_self_check_patch import install as install_board_runtime_self_check

        install_board_runtime_self_check()
    except Exception:
        logger.exception("[startup.entrypoint] board runtime self check install failed")


def system_startup():
    logger.info("🚀 system_startup entry REV25.0-SUMMARY-PENDING-DUP-RESCUE")
    _install_entrypoint_runtime_patches()
    return run_system_startup()


__all__ = [
    "system_startup",
]
