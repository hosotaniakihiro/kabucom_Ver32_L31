from __future__ import annotations
import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)


def _env_default(name: str, value: str) -> None:
    try:
        if os.getenv(name) is None or str(os.getenv(name)).strip() == "":
            os.environ[name] = str(value)
    except Exception:
        pass


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


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


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and not _is_database_collector_context()
    except Exception:
        return False


# Runtime defaults only. These are cheap and safe before main startup.
DEFAULTS = {
    "SUMMARY_AI_PRE_FILTER_DAILY_RISK": "0",
    "SUMMARY_AI_DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS": "1",
    "DAILY_RISK_SYMBOL_STOP_AFTER_FIRST_LOSS": "0",
    "DAILY_RISK_STOP_AFTER_FIRST_LOSS": "0",
    "SYMBOL_STOP_AFTER_FIRST_LOSS_ENABLED": "0",
    "ENTRY_ORDER_EXCHANGE": "9",
    "KABU_ORDER_EXCHANGE": "9",
    "WATCHLIST_RECENT_LIQ_ENABLED": "1",
    "WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN": "1",
    "WATCHLIST_RECENT_LIQ_BULK_SKIP_DB_IN_MAIN": "0",
    "WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT": "0",
    "WATCHLIST_RECENT_LIQ_BULK_TIMEOUT_SEC": "0.75",
    "WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC": "1.0",
    "WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME": "3000",
    "WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME": "3000",
    "WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN": "1000000",
    "FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK": "1",
    "FINAL_ENTRY_TONOSAMA_MIN_VOLUME": "30000",
    "FINAL_ENTRY_TONOSAMA_MIN_TURNOVER": "10000000",
    "ACTIVE_PROTECT_PENDING_SYMBOLS": "1",
    "ACTIVE_PROTECT_EXIT_COOLDOWN_SYMBOLS": "1",
    "ACTIVE_EXIT_COOLDOWN_PROTECT_SEC": "60",
    "ACTIVE_PROTECT_BOARD_RETRY_SYMBOLS": "1",
    "ACTIVE_PROTECT_HOT_SYMBOLS": "1",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME": "30000",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER": "10000000",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE": "200",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE": "0.90",
    "ENTRY_BOARD_MISSING_QTY_RATIO": "0.50",
    "RANKING_TODAY_EMPTY_FAIL_CLOSED": "1",
    "RANKING_ENTRY_STALE_FAIL_CLOSED": "1",
    "RANKING_ENTRY_STALE_FAILOPEN_ENABLED": "0",
    "RANKING_ENTRY_ABORT_ON_STALE": "1",
    "RANKING_ENTRY_CLEAR_PENDING_ON_STALE": "1",
    "RANKING_ENTRY_REQUIRE_TODAY": "1",
    "RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC": "300",
    "RANKING_PRECHECK_MAX_AGE_SEC": "300",
    "RANKING_SNAPSHOT_MAX_AGE_SEC": "300",
    "RANKING_ENTRY_ALLOW_STALE_FALLBACK": "0",
    "RANKING_ENTRY_RAW_FALLBACK_ONLY_TODAY": "1",
    "RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED": "0",
    "RANKING_SNAPSHOT_TECH_BRIDGE_ENABLED": "1",
    "RANKING_AI_GATE_FAILOPEN_ENABLED": "1",
    "RANKING_AI_GATE_FAILOPEN_MIN_SCORE": "50",
    "RANKING_AI_GATE_FAILOPEN_MIN_TURNOVER": "50000000",
    "RANKING_AI_GATE_FAILOPEN_MIN_VOLUME": "30000",
    "ENTRY_RANKING_SCALP_MIN_PRICE": "1500",
    "ENTRY_RANKING_SCALP_MAX_PRICE": "7000",
    "ENTRY_RANKING_SCALP_MIN_MTF": "0.5",
    "ENTRY_RANKING_SCALP_ALLOW_ZERO_MTF_RESCUE": "0",
    "ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG": "0",
    "ENTRY_RANKING_SCALP_RANGE_NO_HIGHLOW_FAILOPEN": "0",
    "ENTRY_RANKING_SCALP_RANGE_ERROR_FAILOPEN": "0",
    "TONOSAMA_STALE_SUMMARY_FAILOPEN": "0",
    "TONOSAMA_RECENT_3M5M_FAILOPEN": "0",
    "TONOSAMA_ALLOW_STALE_MTF_ENTRY": "0",
    "TONOSAMA_MTF_STALE_FAIL_CLOSED": "1",
    "TONOSAMA_SUMMARY_MAX_AGE_SEC": "300",
    "TONOSAMA_HISTORY_MAX_AGE_SEC": "300",
    "SUMMARY_MTF_ENTRY_MAX_AGE_SEC": "300",
    "SUMMARY_SUPPRESS_LUNCH_FALLBACK_AFTER_PM": "1",
    "PULLBACK_ENTRY_ENABLED": "1",
    "PULLBACK_ENTRY_MAX_CANDIDATES": "5",
    "PULLBACK_ENTRY_LOT_RATIO": "0.5",
    "PULLBACK_ENTRY_MIN_PULLBACK_PCT": "0.25",
    "PULLBACK_ENTRY_MAX_PULLBACK_PCT": "1.50",
    "PULLBACK_ENTRY_NEAR_MA_PCT": "0.35",
    "PULLBACK_ENTRY_MIN_REBOUND_VOL_RATIO": "0.80",
    "PULLBACK_ENTRY_MIN_VOLUME": "30000",
    "PULLBACK_ENTRY_MIN_TURNOVER": "10000000",
}
for k, v in DEFAULTS.items():
    _env_default(k, v)

# main.py default restore: entry / exit_loop_5s / ranking / tonosama / summary AI are ON.
# To return to the previous crash-safe mode, set AUTOSTOCK_MAIN_OPERATION_MODE=entry_only before launch.
if _is_main_py():
    for k, v in {
        "AUTOSTOCK_MAIN_OPERATION_MODE": "full",
        "AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS": "0",
        "AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP": "0",
        "AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY": "0",
        "AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY": "0",
        "AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG": "0",
        "AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE": "0",
        "AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK": "0",
        "AUTOSTOCK_MAIN_SKIP_EXIT_LOOP_WHEN_BROKER_EMPTY": "0",
        "AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP": "1",
        "AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK": "1",
        "AUTOSTOCK_MAIN_ENABLE_RANKING_SUMMARY_SCHEDULE": "1",
        "FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK": "1",
    }.items():
        _env_default(k, v)
    logger.warning(
        "[USERCUSTOMIZE] main restore defaults mode=%s exit=%s ranking=%s tonosama=%s summary_ai=%s summary_parent=%s",
        os.getenv("AUTOSTOCK_MAIN_OPERATION_MODE"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK"),
    )

os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = "1"
os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "0"
os.environ["RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE"] = "1"
os.environ["RANKING_SNAPSHOT_TECH_BRIDGE_ENABLED"] = "1"
os.environ["ENTRY_ORDER_EXCHANGE"] = "9"
os.environ["KABU_ORDER_EXCHANGE"] = "9"

logger.warning(
    "[USERCUSTOMIZE] runtime defaults lite ranking_stale_skip=%s ranking_empty_failclosed=%s ranking_tech_bridge=%s tonosama_mtf_stale_fail_closed=%s pullback=%s order_exchange=%s",
    os.getenv("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE"),
    os.getenv("RANKING_TODAY_EMPTY_FAIL_CLOSED"),
    os.getenv("RANKING_SNAPSHOT_TECH_BRIDGE_ENABLED"),
    os.getenv("TONOSAMA_MTF_STALE_FAIL_CLOSED"),
    os.getenv("PULLBACK_ENTRY_ENABLED"),
    os.getenv("ENTRY_ORDER_EXCHANGE"),
)


def _install(label: str, module_name: str) -> None:
    try:
        mod = __import__(module_name, fromlist=["install"])
        fn = getattr(mod, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[USERCUSTOMIZE] %s auto install ok=%s", label, ok)
    except Exception:
        logger.exception("[USERCUSTOMIZE] %s auto install failed", label)


BASE_SYNC_PATCHES = [
    ("PUSH_RECONNECT_STABILITY", "core.startup.push_stream_reconnect_stability_patch"),
    ("PUSH_MAIN_OWNER_POLICY", "core.startup.push_main_owner_lock_policy_patch"),
    ("PUSH_EMPTY_OWNER_FAILOPEN", "core.startup.push_empty_owner_lock_failopen_patch"),
    ("PUSH_ONOPEN_SAFE_REFRESH", "core.startup.push_onopen_refresh_safe_patch"),
    ("RANKING_API_GLOBAL_THROTTLE", "core.startup.ranking_api_global_throttle_patch"),
    ("RANKING_WAL_AGGRESSIVE_TRUNCATE", "core.startup.ranking_wal_aggressive_truncate_patch"),
    ("RANKING_EMPTY_TODAY_FAILCLOSED", "core.startup.ranking_empty_today_failclosed_patch"),
    ("SUMMARY_AFTERNOON_STALE_GUARD", "core.startup.summary_fallback_afternoon_stale_guard_patch"),
]

MAIN_SYNC_PATCHES = [
    ("ORDER_EXCHANGE_SOR", "core.startup.order_exchange_sor_patch"),
    ("REENTRY_STALE_429_EXIT_SAFETY", "core.startup.entry_reentry_stale_429_exit_safety_patch"),
    ("EXIT_TUNING_DEFAULTS", "core.startup.exit_tuning_defaults_patch"),
    ("EXIT_NOISE_CONFIRM_GUARD", "core.startup.exit_noise_confirm_guard_patch"),
    ("EXIT_EXECUTOR_BROKER_V2", "core.startup.exit_executor_broker_fallback_v2_patch"),
    ("SUMMARY_DIFF_STALE_LOCK_GUARD", "core.startup.summary_diff_update_stale_lock_guard_patch"),
    ("ENTRY_LATE_SESSION_TIME_GUARD", "core.startup.entry_late_session_time_guard_patch"),
    ("PULLBACK_ENTRY_PIPELINE", "core.startup.pullback_entry_pipeline_patch"),
    ("RANKING_STALE_SNAPSHOT_SKIP", "core.startup.ranking_entry_stale_snapshot_skip_patch"),
    ("RANKING_SNAPSHOT_TECH_BRIDGE", "core.startup.ranking_entry_snapshot_technical_bridge_patch"),
    ("BOARD_MISSING_PROTECTED_ALLOW", "core.startup.board_missing_protected_allow_patch"),
]

MAIN_BG_PATCHES = [
    ("EXIT_RECENT_PROTECT_MARKER", "core.startup.exit_recent_protect_marker_patch"),
    ("EXIT_DB_STALE_GUARD", "core.startup.exit_db_stale_position_guard_patch"),
    ("EXIT_LOOP_TIMEOUT_GUARD", "core.startup.exit_loop_timeout_guard_patch"),
    ("ENTRY_MA5_THIRD_BAR_GUARD", "core.startup.entry_ma5_third_bar_slope_guard_patch"),
    ("ENTRY_RANKING_SCALP_RESCUE", "core.startup.entry_ranking_scalp_order_rescue_patch"),
    ("RANKING_ENTRY_WIDER_TOP", "core.startup.ranking_entry_wider_top_universe_patch"),
    ("RANKING_WAL_MEMORY_GUARD", "core.startup.ranking_wal_checkpoint_memory_guard_patch"),
    ("TONOSAMA_RUNTIME_25SEC_BUDGET", "core.startup.tonosama_runtime_25sec_budget_patch"),
    ("TONOSAMA_LUNCH_REOPEN_RECENT", "core.startup.tonosama_lunch_reopen_recent_patch"),
    ("YAHOO_COMPUTE_SCHEMA_NA_GUARD", "core.startup.yahoo_compute_schema_na_guard_patch"),
    ("RANKING_ENTRY_FAST_BUDGET_OVERRIDE", "core.startup.ranking_entry_fast_budget_override_patch"),
    ("RANKING_AI_GATE_FAILOPEN", "core.startup.ranking_entry_gate_failopen_patch"),
    ("TONOSAMA_RECENT_3M5M_FAILOPEN", "core.startup.tonosama_recent3m5m_failopen_patch"),
    ("TONOSAMA_FAILOPEN_DIRECTION_RESCUE", "core.startup.tonosama_failopen_direction_rescue_patch"),
    ("TONOSAMA_ATR1M_RESCUE", "core.startup.tonosama_atr1m_filter_rescue_patch"),
    ("TONOSAMA_RANGE5M_RESCUE", "core.startup.tonosama_range5m_filter_rescue_patch"),
    ("TONOSAMA_STALE_SUMMARY_FAILOPEN", "core.startup.tonosama_fresh_summary_stale_failopen_override_patch"),
    ("WATCHLIST_LIQ_EMPTY_FAILOPEN", "core.startup.watchlist_liq_empty_failopen_register_patch"),
    ("TONOSAMA_DEDICATED_OK_FINAL_ACCEPT", "core.startup.tonosama_dedicated_ok_final_accept_patch"),
    ("TONOSAMA_ONE_PENDING", "core.startup.tonosama_one_pending_per_loop_patch"),
    ("TONOSAMA_SKIP_BUILD_WHEN_PENDING_EXISTS", "core.startup.tonosama_skip_build_when_pending_exists_patch"),
    ("TONOSAMA_CONTROLLER_TIMEOUT", "core.startup.tonosama_controller_timeout_patch"),
    ("TONOSAMA_CONTROLLER_TIMEOUT_EXTEND", "core.startup.tonosama_controller_timeout_extend_patch"),
    ("TONOSAMA_RANGE5M_TUPLE_FAILOPEN", "core.startup.tonosama_range_5m_tuple_failopen_patch"),
    ("RANKING_ENTRY_INTRADAY_CAP", "core.startup.ranking_entry_intraday_cap_patch"),
    ("SUMMARY_AI_NO_DIRECT_SYNC", "core.startup.summary_ai_no_direct_sync_patch"),
    ("RANKING_ENTRY_MARKET_HOURS_SKIP", "core.startup.ranking_entry_market_hours_skip_patch"),
    ("RANKING_EMPTY_TODAY_FAILCLOSED_LAST", "core.startup.ranking_empty_today_failclosed_patch"),
    ("RANKING_STALE_SNAPSHOT_SKIP_LAST", "core.startup.ranking_entry_stale_snapshot_skip_patch"),
    ("RANKING_SNAPSHOT_TECH_BRIDGE_LAST", "core.startup.ranking_entry_snapshot_technical_bridge_patch"),
    ("SUMMARY_AFTERNOON_STALE_GUARD_LAST", "core.startup.summary_fallback_afternoon_stale_guard_patch"),
    ("RANKING_AI_GATE_FAILOPEN_LAST", "core.startup.ranking_entry_gate_failopen_patch"),
    ("ENTRY_RANKING_SCALP_RESCUE_LAST", "core.startup.entry_ranking_scalp_order_rescue_patch"),
    ("RANKING_STALE_FINAL_LAST", "core.startup.ranking_entry_stale_failclosed_final_patch"),
]

DB_PATCHES = [
    ("RANKING_WAL_MEMORY_GUARD", "core.startup.ranking_wal_checkpoint_memory_guard_patch"),
    ("YAHOO_COMPUTE_SCHEMA_NA_GUARD", "core.startup.yahoo_compute_schema_na_guard_patch"),
]


def _install_many(items) -> None:
    for label, module_name in items:
        _install(label, module_name)


def _bg_main() -> None:
    logger.warning("[USERCUSTOMIZE] main background patches start count=%s", len(MAIN_BG_PATCHES))
    _install_many(MAIN_BG_PATCHES)
    logger.warning("[USERCUSTOMIZE] main background patches done")


_install_many(BASE_SYNC_PATCHES)

if _is_database_collector_context():
    _install_many(DB_PATCHES)
    logger.warning("[USERCUSTOMIZE] database collector context detected -> heavy entry/tonosama patches skipped argv=%s", sys.argv)
elif _is_main_py() and _env_on("USERCUSTOMIZE_MAIN_LITE", True):
    _install_many(MAIN_SYNC_PATCHES)
    threading.Thread(target=_bg_main, name="usercustomize-main-bg-patches", daemon=True).start()
    logger.warning("[USERCUSTOMIZE] main lite mode enabled sync=%s background=%s", len(MAIN_SYNC_PATCHES), len(MAIN_BG_PATCHES))
else:
    _install_many(MAIN_SYNC_PATCHES + MAIN_BG_PATCHES)
