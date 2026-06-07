# ============================================================
# File   : core/startup/startup.py
# Version: FINAL-PRODUCTION-REV24.6-BOARD-RUNTIME-SAFETY-CLAMP
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
#
# REV23.9:
#   - tonosama_fast_score_prefilter_patch を起動時に明示適用
#   - TONOSAMAのfinal_score_low候補をAI/5秒特徴取得前に早期除外
#
# REV24.0:
#   - rest_full_board_entry_patch を起動時に明示適用
#   - エントリー候補のLIMIT注文だけ REST /board 複数段板で最終価格補正
#
# REV24.1:
#   - exit_limit_pending_close_runtime_patch を起動時に明示適用
#   - exit_unfilled_reprice_runtime_patch を起動時に明示適用
#   - 指値EXITを注文受付だけでCLOSEDにせず、未約定時は取消→再EXITへ回す
#
# REV24.2:
#   - exit_order_fill_confirm_runtime_patch を起動時に明示適用
#   - CLOSING返済注文を監視し、全約定なら正式CLOSEDへ確定
#
# REV24.3:
#   - exit_closing_stale_reconcile_runtime_patch を起動時に明示適用
#   - CLOSINGが長時間残った場合、ブローカー建玉と照合してOPEN/CLOSEDへ救済
#
# REV24.4:
#   - board_settings_env_bridge_patch を最初に明示適用
#   - settings.ini / settings.local.ini の板関連設定を runtime patch の os.getenv へ反映
#
# REV24.5:
#   - board_runtime_diagnostics_patch を設定ブリッジ直後に明示適用
#   - 板関連runtime設定の実効値を起動時に一覧ログ出力
#
# REV24.6:
#   - board_runtime_safety_clamp_patch を診断前に明示適用
#   - 危険な板関連設定値を安全範囲へ補正してから診断ログ出力
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
        from core.startup.rest_full_board_entry_patch import install as install_rest_full_board_entry_patch

        install_rest_full_board_entry_patch()
    except Exception:
        logger.exception("[startup.entrypoint] REST full board entry patch install failed")

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


def system_startup():
    logger.info("🚀 system_startup entry REV24.6-BOARD-RUNTIME-SAFETY-CLAMP")
    _install_entrypoint_runtime_patches()
    return run_system_startup()


__all__ = [
    "system_startup",
]
