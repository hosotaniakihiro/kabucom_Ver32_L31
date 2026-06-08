from __future__ import annotations
import logging
import os
import sys
logger = logging.getLogger(__name__)


def _env_default(name: str, value: str) -> None:
    try:
        if os.getenv(name) is None or str(os.getenv(name)).strip() == "":
            os.environ[name] = str(value)
    except Exception:
        pass


_env_default("SUMMARY_AI_PRE_FILTER_DAILY_RISK", "0")
_env_default("SUMMARY_AI_DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS", "1")
_env_default("DAILY_RISK_SYMBOL_STOP_AFTER_FIRST_LOSS", "0")
_env_default("DAILY_RISK_STOP_AFTER_FIRST_LOSS", "0")
_env_default("SYMBOL_STOP_AFTER_FIRST_LOSS_ENABLED", "0")

_env_default("WATCHLIST_RECENT_LIQ_ENABLED", "1")
_env_default("WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN", "1")
_env_default("WATCHLIST_RECENT_LIQ_BULK_SKIP_DB_IN_MAIN", "0")
_env_default("WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT", "0")
_env_default("WATCHLIST_RECENT_LIQ_BULK_TIMEOUT_SEC", "0.75")
_env_default("WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC", "1.0")
_env_default("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", "3000")
_env_default("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", "3000")
_env_default("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", "1000000")

_env_default("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK", "1")
_env_default("FINAL_ENTRY_TONOSAMA_MIN_VOLUME", "30000")
_env_default("FINAL_ENTRY_TONOSAMA_MIN_TURNOVER", "10000000")

_env_default("ACTIVE_PROTECT_PENDING_SYMBOLS", "1")
_env_default("ACTIVE_PROTECT_EXIT_COOLDOWN_SYMBOLS", "1")
_env_default("ACTIVE_EXIT_COOLDOWN_PROTECT_SEC", "60")
_env_default("ACTIVE_PROTECT_BOARD_RETRY_SYMBOLS", "1")
_env_default("ACTIVE_PROTECT_HOT_SYMBOLS", "1")

os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = "1"
os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "0"
_env_default("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", "30000")
_env_default("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", "10000000")
_env_default("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", "200")
_env_default("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", "0.90")
_env_default("ENTRY_BOARD_MISSING_QTY_RATIO", "0.50")

_env_default("RANKING_TODAY_EMPTY_FAIL_CLOSED", "1")
os.environ["RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE"] = "1"
_env_default("RANKING_ENTRY_STALE_FAIL_CLOSED", "1")
_env_default("RANKING_ENTRY_STALE_FAILOPEN_ENABLED", "0")
_env_default("RANKING_ENTRY_ABORT_ON_STALE", "1")
_env_default("RANKING_ENTRY_CLEAR_PENDING_ON_STALE", "1")
_env_default("RANKING_ENTRY_REQUIRE_TODAY", "1")
_env_default("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", "300")
_env_default("RANKING_PRECHECK_MAX_AGE_SEC", "300")
_env_default("RANKING_SNAPSHOT_MAX_AGE_SEC", "300")
_env_default("RANKING_ENTRY_ALLOW_STALE_FALLBACK", "0")
_env_default("RANKING_ENTRY_RAW_FALLBACK_ONLY_TODAY", "1")
_env_default("RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED", "0")

_env_default("RANKING_AI_GATE_FAILOPEN_ENABLED", "1")
_env_default("RANKING_AI_GATE_FAILOPEN_MIN_SCORE", "50")
_env_default("RANKING_AI_GATE_FAILOPEN_MIN_TURNOVER", "50000000")
_env_default("RANKING_AI_GATE_FAILOPEN_MIN_VOLUME", "30000")
_env_default("ENTRY_RANKING_SCALP_MIN_PRICE", "1500")
_env_default("ENTRY_RANKING_SCALP_MAX_PRICE", "7000")
_env_default("ENTRY_RANKING_SCALP_MIN_MTF", "0.5")
_env_default("ENTRY_RANKING_SCALP_ALLOW_ZERO_MTF_RESCUE", "0")
_env_default("ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG", "0")
_env_default("ENTRY_RANKING_SCALP_RANGE_NO_HIGHLOW_FAILOPEN", "0")
_env_default("ENTRY_RANKING_SCALP_RANGE_ERROR_FAILOPEN", "0")

_env_default("TONOSAMA_STALE_SUMMARY_FAILOPEN", "0")
_env_default("TONOSAMA_RECENT_3M5M_FAILOPEN", "0")
_env_default("TONOSAMA_ALLOW_STALE_MTF_ENTRY", "0")
_env_default("TONOSAMA_MTF_STALE_FAIL_CLOSED", "1")
_env_default("TONOSAMA_SUMMARY_MAX_AGE_SEC", "300")
_env_default("TONOSAMA_HISTORY_MAX_AGE_SEC", "300")
_env_default("SUMMARY_MTF_ENTRY_MAX_AGE_SEC", "300")
_env_default("SUMMARY_SUPPRESS_LUNCH_FALLBACK_AFTER_PM", "1")

_env_default("PULLBACK_ENTRY_ENABLED", "1")
_env_default("PULLBACK_ENTRY_MAX_CANDIDATES", "5")
_env_default("PULLBACK_ENTRY_LOT_RATIO", "0.5")
_env_default("PULLBACK_ENTRY_MIN_PULLBACK_PCT", "0.25")
_env_default("PULLBACK_ENTRY_MAX_PULLBACK_PCT", "1.50")
_env_default("PULLBACK_ENTRY_NEAR_MA_PCT", "0.35")
_env_default("PULLBACK_ENTRY_MIN_REBOUND_VOL_RATIO", "0.80")
_env_default("PULLBACK_ENTRY_MIN_VOLUME", "30000")
_env_default("PULLBACK_ENTRY_MIN_TURNOVER", "10000000")

logger.warning(
    "[USERCUSTOMIZE] runtime defaults applied summary_ai_daily_risk=%s liq_run_in_main=%s liq_fail_open=%s ranking_precheck_pending_failopen=%s ranking_empty_failclosed=%s ranking_stale_skip=%s ranking_stale_failopen=%s ranking_require_today=%s ranking_clear_pending=%s ranking_ai_failopen=%s scalp_price=%s-%s scalp_min_mtf=%s zero_mtf_rescue=%s scalp_ai_any_ng=%s tonosama_mtf_stale_fail_closed=%s suppress_lunch_pm=%s pullback=%s",
    os.getenv("SUMMARY_AI_PRE_FILTER_DAILY_RISK"),
    os.getenv("WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN"),
    os.getenv("WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT"),
    os.getenv("RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED"),
    os.getenv("RANKING_TODAY_EMPTY_FAIL_CLOSED"),
    os.getenv("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE"),
    os.getenv("RANKING_ENTRY_STALE_FAILOPEN_ENABLED"),
    os.getenv("RANKING_ENTRY_REQUIRE_TODAY"),
    os.getenv("RANKING_ENTRY_CLEAR_PENDING_ON_STALE"),
    os.getenv("RANKING_AI_GATE_FAILOPEN_ENABLED"),
    os.getenv("ENTRY_RANKING_SCALP_MIN_PRICE"),
    os.getenv("ENTRY_RANKING_SCALP_MAX_PRICE"),
    os.getenv("ENTRY_RANKING_SCALP_MIN_MTF"),
    os.getenv("ENTRY_RANKING_SCALP_ALLOW_ZERO_MTF_RESCUE"),
    os.getenv("ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG"),
    os.getenv("TONOSAMA_MTF_STALE_FAIL_CLOSED"),
    os.getenv("SUMMARY_SUPPRESS_LUNCH_FALLBACK_AFTER_PM"),
    os.getenv("PULLBACK_ENTRY_ENABLED"),
)


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
            "main_database.py", "db_prepare_runner.py", "ranking_collector_runner.py",
            "push_receiver_runner.py", "yahoo_complement_runner.py", "summary_database_runner.py",
            "data_collectors_runner.py",
        )):
            return True
        return any(os.getenv(k) == "1" for k in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS", "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER", "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
        ))
    except Exception:
        return False


_install("PUSH_RECONNECT_STABILITY", "core.startup.push_stream_reconnect_stability_patch")
_install("PUSH_MAIN_OWNER_POLICY", "core.startup.push_main_owner_lock_policy_patch")
_install("PUSH_EMPTY_OWNER_FAILOPEN", "core.startup.push_empty_owner_lock_failopen_patch")
_install("PUSH_ONOPEN_SAFE_REFRESH", "core.startup.push_onopen_refresh_safe_patch")
_install("RANKING_API_GLOBAL_THROTTLE", "core.startup.ranking_api_global_throttle_patch")
_install("RANKING_WAL_AGGRESSIVE_TRUNCATE", "core.startup.ranking_wal_aggressive_truncate_patch")
_install("RANKING_EMPTY_TODAY_FAILCLOSED", "core.startup.ranking_empty_today_failclosed_patch")
_install("SUMMARY_AFTERNOON_STALE_GUARD", "core.startup.summary_fallback_afternoon_stale_guard_patch")

if _is_database_collector_context():
    _install("RANKING_WAL_MEMORY_GUARD", "core.startup.ranking_wal_checkpoint_memory_guard_patch")
    _install("YAHOO_COMPUTE_SCHEMA_NA_GUARD", "core.startup.yahoo_compute_schema_na_guard_patch")
    logger.warning("[USERCUSTOMIZE] database collector context detected -> heavy entry/tonosama patches skipped argv=%s", sys.argv)
else:
    _install("REENTRY_STALE_429_EXIT_SAFETY", "core.startup.entry_reentry_stale_429_exit_safety_patch")
    _install("EXIT_TUNING_DEFAULTS", "core.startup.exit_tuning_defaults_patch")
    _install("EXIT_NOISE_CONFIRM_GUARD", "core.startup.exit_noise_confirm_guard_patch")
    _install("EXIT_RECENT_PROTECT_MARKER", "core.startup.exit_recent_protect_marker_patch")
    _install("EXIT_EXECUTOR_BROKER_V2", "core.startup.exit_executor_broker_fallback_v2_patch")
    _install("EXIT_DB_STALE_GUARD", "core.startup.exit_db_stale_position_guard_patch")
    _install("EXIT_LOOP_TIMEOUT_GUARD", "core.startup.exit_loop_timeout_guard_patch")
    _install("SUMMARY_DIFF_STALE_LOCK_GUARD", "core.startup.summary_diff_update_stale_lock_guard_patch")
    _install("ENTRY_LATE_SESSION_TIME_GUARD", "core.startup.entry_late_session_time_guard_patch")
    _install("ENTRY_MA5_THIRD_BAR_GUARD", "core.startup.entry_ma5_third_bar_slope_guard_patch")
    _install("PULLBACK_ENTRY_PIPELINE", "core.startup.pullback_entry_pipeline_patch")
    _install("RANKING_STALE_SNAPSHOT_SKIP", "core.startup.ranking_entry_stale_snapshot_skip_patch")
    _install("ENTRY_RANKING_SCALP_RESCUE", "core.startup.entry_ranking_scalp_order_rescue_patch")
    _install("RANKING_ENTRY_WIDER_TOP", "core.startup.ranking_entry_wider_top_universe_patch")
    _install("RANKING_WAL_MEMORY_GUARD", "core.startup.ranking_wal_checkpoint_memory_guard_patch")
    _install("TONOSAMA_RUNTIME_25SEC_BUDGET", "core.startup.tonosama_runtime_25sec_budget_patch")
    _install("TONOSAMA_LUNCH_REOPEN_RECENT", "core.startup.tonosama_lunch_reopen_recent_patch")
    _install("YAHOO_COMPUTE_SCHEMA_NA_GUARD", "core.startup.yahoo_compute_schema_na_guard_patch")
    _install("RANKING_ENTRY_FAST_BUDGET_OVERRIDE", "core.startup.ranking_entry_fast_budget_override_patch")
    _install("RANKING_AI_GATE_FAILOPEN", "core.startup.ranking_entry_gate_failopen_patch")
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
    _install("RANKING_EMPTY_TODAY_FAILCLOSED_LAST", "core.startup.ranking_empty_today_failclosed_patch")
    _install("RANKING_STALE_SNAPSHOT_SKIP_LAST", "core.startup.ranking_entry_stale_snapshot_skip_patch")
    _install("SUMMARY_AFTERNOON_STALE_GUARD_LAST", "core.startup.summary_fallback_afternoon_stale_guard_patch")
    _install("BOARD_MISSING_PROTECTED_ALLOW", "core.startup.board_missing_protected_allow_patch")
    _install("RANKING_AI_GATE_FAILOPEN_LAST", "core.startup.ranking_entry_gate_failopen_patch")
    _install("ENTRY_RANKING_SCALP_RESCUE_LAST", "core.startup.entry_ranking_scalp_order_rescue_patch")
    _install("RANKING_STALE_FINAL_LAST", "core.startup.ranking_entry_stale_failclosed_final_patch")
