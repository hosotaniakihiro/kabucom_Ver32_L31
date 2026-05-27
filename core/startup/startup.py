# ============================================================
# File   : core/startup/startup.py
# Version: FINAL-PRODUCTION-REV23.3-DAILY-MTF-SRC-ALIAS-PATCH
# ------------------------------------------------------------
# 【概要】
#   system_startup の公開入口
#
# 【設計】
#   - このファイルは起動入口だけ
#   - 実際の起動順序は startup_orchestrator.py に委譲
#   - 詳細処理は push_startup / scheduler_startup / summary_startup 等へ分離
#
# REV23.3:
#   - tonosama_history_missing_guard_patch を起動時に明示適用
#   - summary_seed_recent_merged_guard_patch を起動時に明示適用
#   - daily_mtf_daily_src_alias_patch を起動時に明示適用
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


def system_startup():
    logger.info("🚀 system_startup entry REV23.3-DAILY-MTF-SRC-ALIAS-PATCH")
    _install_entrypoint_runtime_patches()
    return run_system_startup()


__all__ = [
    "system_startup",
]
