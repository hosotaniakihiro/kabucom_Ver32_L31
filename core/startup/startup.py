# ============================================================
# File   : core/startup/startup.py
# Version: FINAL-PRODUCTION-REV23.8-RANKING-ENTRY-FAST-PATCH
# ------------------------------------------------------------
# 【概要】
#   system_startup の公開入口
#
# 【設計】
#   - このファイルは起動入口だけ
#   - 実際の起動順序は startup_orchestrator.py に委譲
#   - 詳細処理は push_startup / scheduler_startup / summary_startup 等へ分離
#
# REV23.4:
#   - tonosama_history_missing_guard_patch を起動時に明示適用
#   - summary_seed_recent_merged_guard_patch を起動時に明示適用
#   - daily_mtf_daily_src_alias_patch を起動時に明示適用
#   - summary_controller_publish_mtf_merged_patch を起動時に明示適用
#
# REV23.5:
#   - summary_push_bg_due_interval_guard_patch を起動時に明示適用
#   - main.py(entry_only) のPUSH BGで3分/5分足を毎分投入しない
#
# REV23.6:
#   - entry_pipeline_pending_root_prefilter_patch を起動時に明示適用
#   - TONOSAMA実行時にRANKING/SUMMARY pendingを全銘柄scanしない
#
# REV23.7:
#   - entry_volume_direction_guard_patch を起動時に明示適用
#   - SUMMARY/RANKING/TONOSAMA共通で「出来高急増×価格方向」を判定
#
# REV23.8:
#   - ranking_entry_fast_runtime_patch を起動時に明示適用
#   - RANKING entry作成の80秒超過を軽減
# ============================================================

from __future__ import annotations

import logging

from core.startup.startup_orchestrator import run_system_startup

logger = logging.getLogger(__name__)


def _install_entrypoint_runtime_patches() -> None:
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


def system_startup():
    logger.info("🚀 system_startup entry REV23.8-RANKING-ENTRY-FAST-PATCH")
    _install_entrypoint_runtime_patches()
    return run_system_startup()


__all__ = [
    "system_startup",
]
