from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

def _install(label: str, module_name: str) -> None:
    try:
        mod = __import__(module_name, fromlist=["install"])
        fn = getattr(mod, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[USERCUSTOMIZE] %s auto install ok=%s", label, ok)
    except Exception:
        logger.exception("[USERCUSTOMIZE] %s auto install failed", label)

_install("TONOSAMA_LUNCH_REOPEN_RECENT", "core.startup.tonosama_lunch_reopen_recent_patch")
_install("YAHOO_COMPUTE_SCHEMA_NA_GUARD", "core.startup.yahoo_compute_schema_na_guard_patch")
_install("RANKING_ENTRY_FAST_BUDGET_OVERRIDE", "core.startup.ranking_entry_fast_budget_override_patch")
_install("TONOSAMA_RECENT_3M5M_FAILOPEN", "core.startup.tonosama_recent3m5m_failopen_patch")
_install("TONOSAMA_FAILOPEN_DIRECTION_RESCUE", "core.startup.tonosama_failopen_direction_rescue_patch")
_install("WATCHLIST_LIQ_EMPTY_FAILOPEN", "core.startup.watchlist_liq_empty_failopen_register_patch")
_install("TONOSAMA_DEDICATED_OK_FINAL_ACCEPT", "core.startup.tonosama_dedicated_ok_final_accept_patch")
_install("TONOSAMA_ONE_PENDING", "core.startup.tonosama_one_pending_per_loop_patch")
_install("TONOSAMA_SKIP_BUILD_WHEN_PENDING_EXISTS", "core.startup.tonosama_skip_build_when_pending_exists_patch")
_install("TONOSAMA_CONTROLLER_TIMEOUT", "core.startup.tonosama_controller_timeout_patch")
_install("TONOSAMA_CONTROLLER_TIMEOUT_EXTEND", "core.startup.tonosama_controller_timeout_extend_patch")
_install("RANKING_ENTRY_INTRADAY_CAP", "core.startup.ranking_entry_intraday_cap_patch")
_install("SUMMARY_AI_NO_DIRECT_SYNC", "core.startup.summary_ai_no_direct_sync_patch")
_install("RANKING_ENTRY_MARKET_HOURS_SKIP", "core.startup.ranking_entry_market_hours_skip_patch")
_install("YAHOO_STALE_RESET_OUTSIDE_SESSION", "core.startup.yahoo_stale_reset_outside_session_patch")
_install("RANKING_SUMMARY_MARKET_HOURS_SKIP", "core.startup.ranking_summary_market_hours_skip_patch")
_install("TONOSAMA_MARKET_HOURS_SKIP", "core.startup.tonosama_market_hours_skip_patch")
