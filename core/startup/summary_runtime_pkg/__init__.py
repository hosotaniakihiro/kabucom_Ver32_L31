# ============================================================
# File   : core/startup/summary_runtime_pkg/__init__.py
# Version: REV3.0-SUMMARY-RUNTIME-PKG-EXPORTS
# ------------------------------------------------------------
# 【概要】
#   summary runtime package の公開入口
#
# 【公開API】
#   - bootstrap flags
#   - summary DB seed
#   - bootstrap sync/async
#   - closed-day fallback
# ============================================================

from __future__ import annotations

from .state import (
    set_summary_bootstrap_flags,
    get_summary_bootstrap_state,
    is_summary_bootstrap_running,
)

from .db_seed import (
    seed_runtime_summary_cache_from_db,
)

from .runtime import (
    run_bootstrap_summary_sync,
    start_bootstrap_summary_async,
    run_bootstrap_summary_fast_boot,
)

from .closed_day import (
    rebuild_closed_day_summary_for_display_fallback,
    maybe_prepare_closed_day_display_cache,
    rebuild_closed_day_all_if_available,
    rebuild_closed_day_summaries_all,
)

__all__ = [
    "set_summary_bootstrap_flags",
    "get_summary_bootstrap_state",
    "is_summary_bootstrap_running",
    "seed_runtime_summary_cache_from_db",
    "run_bootstrap_summary_sync",
    "start_bootstrap_summary_async",
    "run_bootstrap_summary_fast_boot",
    "rebuild_closed_day_summary_for_display_fallback",
    "maybe_prepare_closed_day_display_cache",
    "rebuild_closed_day_all_if_available",
    "rebuild_closed_day_summaries_all",
]