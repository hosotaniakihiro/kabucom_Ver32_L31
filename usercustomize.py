from __future__ import annotations
import logging
import os
import sys
logger = logging.getLogger(__name__)

def _install(label: str, module_name: str) -> None:
    try:
        mod = __import__(module_name, fromlist=["install"])
        fn = getattr(mod, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[USERCUSTOMIZE] %s auto install ok=%s", label, ok)
    except Exception:
        logger.exception("[USERCUSTOMIZE] %s auto install failed", label)

def _is_database_collector_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in (
            "main_database.py",
            "db_prepare_runner.py",
            "ranking_collector_runner.py",
            "push_receiver_runner.py",
            "yahoo_complement_runner.py",
            "summary_database_runner.py",
            "data_collectors_runner.py",
        )):
            return True
        if os.getenv("AUTOSTOCK_DATA_COLLECTORS_PROCESS") == "1":
            return True
        if os.getenv("AUTOSTOCK_MAIN_DATABASE_PROCESS") == "1":
            return True
        if os.getenv("AUTOSTOCK_SUMMARY_DB_WRITER") == "1":
            return True
        if os.getenv("AUTOSTOCK_RANKING_COLLECTOR_PROCESS") == "1":
            return True
    except Exception:
        pass
    return False

_install("PUSH_RECONNECT_STABILITY", "core.startup.push_stream_reconnect_stability_patch")
_install("PUSH_MAIN_OWNER_POLICY", "core.startup.push_main_owner_lock_policy_patch")
_install("PUSH_EMPTY_OWNER_FAILOPEN", "core.startup.push_empty_owner_lock_failopen_patch")
_install("PUSH_ONOPEN_SAFE_REFRESH", "core.startup.push_onopen_refresh_safe_patch")
_install("RANKING_WAL_AGGRESSIVE_TRUNCATE", "core.startup.ranking_wal_aggressive_truncate_patch")

if _is_database_collector_context():
    _install("RANKING_WAL_MEMORY_GUARD", "core.startup.ranking_wal_checkpoint_memory_guard_patch")
    _install("YAHOO_COMPUTE_SCHEMA_NA_GUARD", "core.startup.yahoo_compute_schema_na_guard_patch")
    logger.warning("[USERCUSTOMIZE] database collector context detected -> heavy entry/tonosama patches skipped argv=%s", sys.argv)
else:
    _install("REENTRY_STALE_429_EXIT_SAFETY", "core.startup.entry_reentry_stale_429_exit_safety_patch")
    _install("EXIT_TUNING_DEFAULTS", "core.startup.exit_tuning_defaults_patch")
    _install("EXIT_NOISE_CONFIRM_GUARD", "core.startup.exit_noise_confirm_guard_patch")
    _install("EXIT_EXECUTOR_BROKER_V2", "core.startup.exit_executor_broker_fallback_v2_patch")
    _install("EXIT_DB_STALE_GUARD", "core.startup.exit_db_stale_position_guard_patch")
    _install("EXIT_LOOP_TIMEOUT_GUARD", "core.startup.exit_loop_timeout_guard_patch")
    _install("SUMMARY_DIFF_STALE_LOCK_GUARD", "core.startup.summary_diff_update_stale_lock_guard_patch")
    _install("ENTRY_LATE_SESSION_TIME_GUARD", "core.startup.entry_late_session_time_guard_patch")
    _install("ENTRY_MA5_THIRD_BAR_GUARD", "core.startup.entry_ma5_third_bar_slope_guard_patch")
    _install("ENTRY_RANKING_SCALP_RESCUE", "core.startup.entry_ranking_scalp_order_rescue_patch")
    _install("RANKING_ENTRY_WIDER_TOP", "core.startup.ranking_entry_wider_top_universe_patch")
    _install("RANKING_WAL_MEMORY_GUARD", "core.startup.ranking_wal_checkpoint_memory_guard_patch")
    _install("TONOSAMA_RUNTIME_25SEC_BUDGET", "core.startup.tonosama_runtime_25sec_budget_patch")
    _install("TONOSAMA_LUNCH_REOPEN_RECENT", "core.startup.tonosama_lunch_reopen_recent_patch")
    _install("YAHOO_COMPUTE_SCHEMA_NA_GUARD", "core.startup.yahoo_compute_schema_na_guard_patch")
    _install("RANKING_ENTRY_FAST_BUDGET_OVERRIDE", "core.startup.ranking_entry_fast_budget_override_patch")
    _install("TONOSAMA_RECENT_3M5M_FAILOPEN", "core.startup.tonosama_recent3m5m_failopen_patch")
    _install("TONOSAMA_FAILOPEN_DIRECTION_RESCUE", "core.startup.tonosama_failopen_direction_rescue_patch")
    _install("TONOSAMA_ATR1M_RESCUE", "core.startup.tonosama_atr1m_filter_rescue_patch")
    _install("TONOSAMA_RANGE5M_RESCUE", "core.startup.tonosama_range5m_filter_rescue_patch")
    _install("TONOSAMA_STALE_SUMMARY_FAILOPEN", "core.startup.tonosama_fresh_summary_stale_failopen_override_patch")
    _install("WATCHLIST_LIQ_EMPTY_FAILOPEN", "core.startup.watchlist_liq_empty_failopen_register_patch")
    _install("TONOSAMA_DEDICATED_OK_FINAL_ACCEPT", "core.startup.tonosama_dedicated_ok_final_accept_patch")
    _install("TONOSAMA_ONE_PENDING", "core.startup.tonosama_one_pending_per_loop_patch")
    _install("TONOSAMA_SKIP_BUILD_WHEN_PENDING_EXISTS", "core.startup.tonosama_skip_build_when_pending_exists_patch")
    _install("TONOSAMA_CONTROLLER_TIMEOUT", "core.startup.tonosama_controller_timeout_patch")
    _install("TONOSAMA_CONTROLLER_TIMEOUT_EXTEND", "core.startup.tonosama_controller_timeout_extend_patch")
    _install("TONOSAMA_RANGE5M_TUPLE_FAILOPEN", "core.startup.tonosama_range_5m_tuple_failopen_patch")
    _install("RANKING_ENTRY_INTRADAY_CAP", "core.startup.ranking_entry_intraday_cap_patch")
    _install("SUMMARY_AI_NO_DIRECT_SYNC", "core.startup.summary_ai_no_direct_sync_patch")
    _install("RANKING_ENTRY_MARKET_HOURS_SKIP", "core.startup.ranking_entry_market_hours_skip_patch")