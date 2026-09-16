# -*- coding: utf-8 -*-
"""
Centralized runtime environment defaults for startup customization.

This module is intentionally conservative:
- It only fills missing environment variables by default.
- Existing operator/task-scheduler settings always win.
- sitecustomize.py / usercustomize.py can import this module safely.

The goal is to gradually move scattered os.environ.setdefault(...) calls
from sitecustomize.py, usercustomize.py, and compatibility patches into one
small, auditable place.
"""
from __future__ import annotations

import os
from typing import Dict, Mapping, MutableMapping, Optional

VERSION = "REV7-RUNTIME-ENV-DEFAULTS-COMPAT-WRAPPERS-SUMMARY-PRICE-CAP"

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    val = str(raw).strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return bool(default)


def _set_defaults(defaults: Mapping[str, str], *, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    env = environ if environ is not None else os.environ
    applied: Dict[str, str] = {}
    for key, value in defaults.items():
        if key not in env:
            env[key] = str(value)
            applied[key] = str(value)
    return applied


def _merge_applied(*items: Dict[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for item in items:
        merged.update(item or {})
    return merged


PUSH_DEFAULTS: Dict[str, str] = {
    "PUSH_WS_VENDOR_RUN_FOREVER": "1",
    "PUSH_WS_ENABLE_PING": "0",
    "PUSH_ROTATION_CLOSE_BEFORE_REGISTER": "0",
    "PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER": "0",
    # 2026-09-02 の reconnect storm (consecutive=170) の原因。
    #
    # rotation_A / rotation_B の登録は **REST の PUT /kabusapi/register** で行う。
    # WebSocket を使わないので、WS が切れている間でも実行できる
    # (transport.py REV1.6 のコメントがまさにそう書いている)。
    # ところがここが "0" だと transport._allow_refresh_with_ws_closed() が False になり、
    #   [push_stream] refresh skipped reason=rotation_A ws_not_ready
    # で登録そのものが行われない。
    #
    # PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1 により on_open 側でも登録しないため、
    # **銘柄登録を行う経路が 1 つも残らない**。実測 (09:14:30 起動):
    #   09:15:10 rotation A register failed (ws_not_ready) failure_count=1 -> backoff 180s
    #   以降 600s backoff で 10 分に 1 回だけ再試行し、毎回同じ理由で失敗
    #   その間 WS は 3〜7 秒で切られ続け generation=171 まで到達
    # 登録が無い -> kabu Station が PUSH を切る -> 切れているから登録できない、
    # という相互ロックになっていた。
    #
    # REST 登録は WS の状態に依存しないので "1" が本来の設計値。
    "PUSH_ROTATION_REGISTER_WITH_WS_CLOSED": "1",
    "PUSH_ROTATION_WAIT_WS_READY_AFTER_REGISTER": "1",
    "PUSH_ROTATION_HOLD_SEC": "4.8",
    "PUSH_ROTATION_CLEAR_GAP_SEC": "0.2",
    "PUSH_ROTATION_WS_CLOSE_SETTLE_SEC": "0.15",
    "PUSH_ROTATION_UNREGISTER_WAIT_SEC": "0.2",
    "PUSH_ROTATION_POST_REGISTER_WS_READY_TIMEOUT_SEC": "4.0",
    "PUSH_ROTATION_POST_REGISTER_WS_POLL_SEC": "0.05",
    "PUSH_ROTATION_POST_REGISTER_WS_SETTLE_SEC": "0.25",
    "PUSH_ROTATION_FAILURE_BACKOFF_SEC": "180",
    "PUSH_ROTATION_FAILURE_BACKOFF_MAX_SEC": "600",
    "PUSH_ROTATION_FAILURE_BACKOFF_MULTIPLIER": "1.5",
    "PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH": "1",
    "PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST": "0",
    "PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST": "0",
    "PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR": "0.0",
    "PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC": "0.50",
    "PUSH_STREAM_ONOPEN_WS_READY_TIMEOUT_SEC": "1.0",
    "PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER": "1",
    "PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC": "0.3",
    # 即死が続くときの上限。1.0 だと 300 回連続でも 1 秒間隔で撃ち続けるため 15 秒まで許す。
    "PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC": "15.0",
    "PUSH_STREAM_RECONNECT_BACKOFF_LADDER_SEC": "1,2,4,8,15",
    "PUSH_STREAM_RECONNECT_STABLE_RESET_SEC": "5.0",
    "PUSH_STREAM_SHORT_LIVED_SEC": "1.0",
    "PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC": "0.0",
    "PUSH_STREAM_SINGLE_OWNER_LOCK": "1",
    # kabu Station の PUSH は 1 セッション 1 接続。OS レベルの single-instance lock で担保する。
    "KABU_PUSH_WS_OWNER_LOCK": "1",
    "KABU_REGISTER_REQUIRE_WS_OWNER": "1",
    "PUSH_ROTATION_REQUIRE_WS_OWNER": "1",
    "ATS_REGISTER_REQUIRE_WS_OWNER": "1",
    "PUSH_STREAM_SINGLE_OWNER_WAIT_RETRY": "1",
    "PUSH_STREAM_SINGLE_OWNER_RETRY_SEC": "1.0",
    "PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC": "20.0",
    "PUSH_STREAM_EMPTY_OWNER_LOCK_FAIL_OPEN": "1",
    "PUSH_STREAM_EMPTY_OWNER_LOCK_REQUIRE_OWNER_CONTEXT": "1",
    "ACTIVE_PREMARKET_ALLOW_NO_PRICE": "1",
    "PUSH_REGISTER_LIQUIDITY_ROTATION_PRESERVE_FULL_POOL": "0",
    "PUSH_REGISTER_LIQUIDITY_ROTATION_MIN_SURVIVOR_COUNT": "50",
    "PUSH_REGISTER_LIQUIDITY_ROTATION_MIN_SURVIVOR_RATIO": "0.50",
    "USERCUSTOMIZE_ENABLE_LEGACY_PUSH_PATCHES": "0",
}

RESCUE_DEFAULTS: Dict[str, str] = {
    "SITECUSTOMIZE_ENABLE_RESCUE_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_ENTRY_FAILOPEN_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_RANKING_FINAL_RESCUE_PATCH": "0",
    "SITECUSTOMIZE_ENABLE_TONOSAMA_EXTRA_RESCUE_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_SUMMARY_AI_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_LEGACY_RANKING_FAILOPEN_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_LEGACY_TONOSAMA_FAILOPEN_PATCHES": "0",
    "RANKING_AI_GATE_FAILOPEN_ENABLED": "0",
    "RANKING_FINAL_RESCUE_AI_FAILOPEN": "0",
    "ENTRY_DIRECTION_RECURSION_FAILOPEN_ENABLED": "0",
    "TONOSAMA_FAILOPEN_DIRECTION_RESCUE": "0",
    "TONOSAMA_ATR1M_FILTER_RESCUE": "0",
    "TONOSAMA_RANGE5M_FILTER_RESCUE": "0",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING": "0",
    "TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE": "0",
    "TONOSAMA_DROP_HISTORY_MISSING_ENTRY": "1",
    "LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK": "0",
}

DB_DEFAULTS: Dict[str, str] = {
    "SQLITE_MEMORY_PRAGMAS_ENABLED": "1",
    "SQLITE_MEMORY_TEMP_STORE": "MEMORY",
    "SQLITE_MEMORY_CACHE_KB": "-65536",
    "SQLITE_BUSY_TIMEOUT_MS": "5000",
    "SQLITE_MMAP_SIZE_BYTES": "268435456",
    "SQLITE_CACHE_SPILL_OFF": "1",
    "SQLITE_WAL_AUTOCHECKPOINT": "1000",
    "SQLITE_SYNCHRONOUS": "NORMAL",
    # 旧 core/startup/summary_db_lock_pressure_patch.py の install() が
    # setdefault していた値。upsert_executor が import 時に読むため、
    # sitecustomize から最も早く適用されるここで確定させる。
    # 同パッチは SQLITE_BUSY_TIMEOUT_MS も 45000 で setdefault していたが、
    # apply_site_defaults がこの dict を先に適用するため上の 5000 が勝っており
    # 実際には不発だった。挙動を変えないためここには入れない。
    "SUMMARY_REALTIME_FORCE_LATEST_ONLY": "1",
    "SUMMARY_REALTIME_SKIP_IF_BUSY": "1",
    "SUMMARY_REALTIME_LOCK_TIMEOUT_SEC": "2.0",
    "SUMMARY_REALTIME_LATEST_ONLY_ROW_THRESHOLD": "500",
    "SUMMARY_UPSERT_WAL_CHECKPOINT": "0",
    "SUMMARY_UPSERT_CHUNK_SIZE": "250",
    "SUMMARY_UPSERT_RETRY": "6",
    "SUMMARY_UPSERT_LOCK_BACKOFF_MAX_SEC": "1.2",
}

HELPER_DEFAULTS: Dict[str, str] = {
    "SITECUSTOMIZE_ENABLE_FULL_NONMAIN": "0",
}

MAIN_RESTORE_DEFAULTS: Dict[str, str] = {
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
}

RANKING_ENTRY_DEFAULTS: Dict[str, str] = {
    "RANKING_ENTRY_WATCHDOG_ENABLED": "1",
    "RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC": "55",
    "RANKING_ENTRY_HARD_TIMEOUT_ENABLED": "1",
    "RANKING_ENTRY_HARD_TIMEOUT_SEC": "28",
    "RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC": "15",
    "RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC": "18",
    "RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC": "12",
    "RANKING_ENTRY_SNAPSHOT_TECH_ALIAS_ENABLED": "1",
    "RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED": "1",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_PATCH_ENABLED": "1",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_LOOKBACK_ROWS": "12",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_MAX_AGE_MIN": "30",
    "RANKING_STUCK_PENDING_MAX_CONTROLLER_RETRY": "2",
    "RANKING_STUCK_PENDING_MAX_AGE_SEC": "120",
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
    "RANKING_AI_GATE_FAILOPEN_MIN_SCORE": "50",
    "RANKING_AI_GATE_FAILOPEN_MIN_TURNOVER": "30000000",
    "RANKING_AI_GATE_FAILOPEN_MIN_VOLUME": "30000",
    "RANKING_ENTRY_LIGHT_MIN_SCORE": "50",
    "RANKING_ENTRY_LIGHT_MIN_TURNOVER": "30000000",
    "RANKING_FINAL_RESCUE_MIN_SCORE": "50",
    "RANKING_FINAL_RESCUE_MIN_TURNOVER": "30000000",
    "RANKING_FINAL_RESCUE_ATR_MIN_RATIO": "0.0005",
    "LOW_MOVE_RANKING_MIN_ENTRY_PRICE": "300",
    "LOW_MOVE_RANKING_MAX_ENTRY_PRICE": "7000",
    "LOW_MOVE_RANKING_MIN_RANGE_PCT_LOW_PRICE": "0.008",
    "LOW_MOVE_RANKING_MIN_RANGE_PCT_HIGH_PRICE": "0.006",
    "LOW_MOVE_RANKING_STRONG_RANGE_PCT": "0.014",
    "LOW_MOVE_RANKING_MIN_ABS_SLOPE": "0.0000",
    "LOW_MOVE_RANKING_ZERO_ATR_MIN_SCORE": "50",
    "LOW_MOVE_RANKING_MIN_SCORE_FOR_NO_HIGHLOW": "50",
    "LOW_MOVE_RANKING_ZERO_ATR_MIN_TURNOVER": "30000000",
    "ENTRY_RANKING_SCALP_MIN_PRICE": "1500",
    "ENTRY_RANKING_SCALP_MAX_PRICE": "7000",
    "ENTRY_RANKING_SCALP_MIN_MTF": "0.5",
    "ENTRY_RANKING_SCALP_ALLOW_ZERO_MTF_RESCUE": "0",
    "ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG": "0",
    "ENTRY_RANKING_SCALP_RANGE_NO_HIGHLOW_FAILOPEN": "0",
    "ENTRY_RANKING_SCALP_RANGE_ERROR_FAILOPEN": "0",
    "USERCUSTOMIZE_ALLOW_DUPLICATE_PATCHES": "0",
}

TONOSAMA_DEFAULTS: Dict[str, str] = {
    "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE": "3.0",
    "TONOSAMA_5SEC_ADVISORY_ENABLED": "1",
    "TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS": "0",
    "TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC": "1",
    "TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT": "0.0",
    "TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX": "1",
    "TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT": "0.50",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_ENABLED": "0",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_RANGE_PCT": "3.0",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_VOLUME": "50000",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE": "3.0",
    "TONOSAMA_ENTRY_TIMEOUT_SEC": "45",
    "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC": "12",
    "TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING": "1",
    "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC": "10",
    "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC": "60",
    "ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE": "1",
    "ENTRY_CONTROLLER_TONOSAMA_MIN_PENDING_SCORE": "1.0",
    "TONOSAMA_ORDER_MAX_AGE_SEC": "240",
    "FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK": "1",
    "FINAL_ENTRY_TONOSAMA_MIN_VOLUME": "30000",
    "FINAL_ENTRY_TONOSAMA_MIN_TURNOVER": "10000000",
    "TONOSAMA_STALE_SUMMARY_FAILOPEN": "0",
    "TONOSAMA_RECENT_3M5M_FAILOPEN": "0",
    "TONOSAMA_ALLOW_STALE_MTF_ENTRY": "0",
    "TONOSAMA_MTF_STALE_FAIL_CLOSED": "1",
    "TONOSAMA_SUMMARY_MAX_AGE_SEC": "300",
    "TONOSAMA_HISTORY_MAX_AGE_SEC": "300",
    "SUMMARY_MTF_ENTRY_MAX_AGE_SEC": "300",
}

ENTRY_DEFAULTS: Dict[str, str] = {
    "ENTRY_CONTROLLER_LOCK_WAIT_ENABLED": "1",
    "ENTRY_CONTROLLER_LOCK_WAIT_SOURCES": "RANKING,TONOSAMA,SUMMARY",
    "ENTRY_CONTROLLER_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED": "1",
    "ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL": "1",
    "ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED": "1",
    "ENTRY_SHORT_MTF_REQUIRED": "1",
    "ENTRY_SHORT_MTF_FORCE_2OF3": "1",
    "ENTRY_SHORT_MTF_MIN_ALIGNED": "2",
    "ENTRY_SHORT_MTF_MIN_AVAILABLE": "2",
    "ENTRY_SHORT_MTF_SLOPE_EPS": "0.0",
    "ENTRY_DAILY_MTF_OPTIONAL": "1",
    "ENTRY_SHORT_MTF_DB_BACKFILL": "1",
    "ENTRY_SHORT_MTF_ZERO_NEUTRAL": "1",
    "ENTRY_ORDER_EXCHANGE": "9",
    "KABU_ORDER_EXCHANGE": "9",
    "SUMMARY_AI_PRE_FILTER_DAILY_RISK": "0",
    "SUMMARY_AI_DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS": "1",
    "DAILY_RISK_SYMBOL_STOP_AFTER_FIRST_LOSS": "0",
    "DAILY_RISK_STOP_AFTER_FIRST_LOSS": "0",
    "SYMBOL_STOP_AFTER_FIRST_LOSS_ENABLED": "0",
    "WATCHLIST_RECENT_LIQ_ENABLED": "1",
    "WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN": "1",
    "WATCHLIST_RECENT_LIQ_BULK_SKIP_DB_IN_MAIN": "0",
    "WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT": "0",
    "WATCHLIST_RECENT_LIQ_BULK_TIMEOUT_SEC": "0.75",
    "WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC": "1.0",
    "WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME": "3000",
    "WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME": "3000",
    "WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN": "1000000",
    "ACTIVE_PROTECT_PENDING_SYMBOLS": "1",
    "ACTIVE_PROTECT_EXIT_COOLDOWN_SYMBOLS": "1",
    "ACTIVE_EXIT_COOLDOWN_PROTECT_SEC": "60",
    "ACTIVE_PROTECT_BOARD_RETRY_SYMBOLS": "1",
    "ACTIVE_PROTECT_HOT_SYMBOLS": "1",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME": "30000",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER": "10000000",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE": "200",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE": "0.90",
    # 2026-09-15: 1 分足の方向確認ガード (close vs ma5/ma25 の構造・乖離) は
    #   3 分/5 分足の MA/RSI/MACD ゲート (trading/filters/mtf_ma_gate) と役割が重複し、
    #   ゲート通過後に 1 分の揺れで落としていた ([ENTRY DIRECTION SAFETY] BLOCK
    #   original_guard_ng が 9/15 に多発)。既定で無効化。戻すときは env で 1。
    "ENTRY_DIRECTION_CONFIRM_ENABLED": "0",
    # 2026-09-16: 「3分/5分足で MA5 を超えて 3 本目、かつ MA5 が順行」を要求するガード。
    #   3m/5m ゲート (終値>5MA>25MA, RSI, MACD) を通った候補をさらに 9 分待たせ、
    #   in-memory の古い 3m 履歴で判定していた (4075: closes [1473,1608,1618] で NG)。
    #   9/16 午前、ゲート通過後の唯一の候補をこれが落とした。既定で無効化。
    "ENTRY_MA5_THIRD_BAR_SLOPE_GUARD_ENABLED": "0",
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

SUMMARY_YAHOO_DEFAULTS: Dict[str, str] = {
    "AUTOSTOCK_MAIN_SKIP_SUMMARY_DB_SAVE": "1",
    "AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT": "1",
    "YAHOO_COMPLEMENT_RUN_IN_MAIN": "0",
    "AUTOSTOCK_ENABLE_YAHOO_COMPLEMENT_IN_MAIN": "0",
    "YAHOO_COMPLEMENT_DB_WARMUP_ENABLED": "1",
    "YAHOO_COMPLEMENT_DB_WARMUP_MIN_BARS": "75",
    "YAHOO_COMPLEMENT_DB_WARMUP_LOOKBACK_DAYS": "7",
    "SUMMARY_DB_DATE_GUARD_ENABLED": "1",
    "SUMMARY_DB_DATE_GUARD_CLEANUP_ENABLED": "0",
    "SUMMARY_SUPPRESS_LUNCH_FALLBACK_AFTER_PM": "1",
    "TRADE_UNIVERSE_MIN_PRICE": "200",
    "TRADE_UNIVERSE_MAX_PRICE": "7000",
    "SUMMARY_PIPELINE_MIN_PRICE": "200",
    "SUMMARY_PIPELINE_MAX_PRICE": "7000",
    "SUMMARY_DISPLAY_MAX_PRICE": "7000",
    "ENTRY_MAX_PRICE": "7000",
    "RANKING_MAX_PRICE": "7000",
}


def apply_push_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(PUSH_DEFAULTS, environ=environ)


def apply_rescue_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(RESCUE_DEFAULTS, environ=environ)


def apply_db_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(DB_DEFAULTS, environ=environ)


def apply_helper_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(HELPER_DEFAULTS, environ=environ)


def apply_main_restore_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(MAIN_RESTORE_DEFAULTS, environ=environ)


def apply_ranking_entry_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(RANKING_ENTRY_DEFAULTS, environ=environ)


def apply_tonosama_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(TONOSAMA_DEFAULTS, environ=environ)


def apply_entry_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(ENTRY_DEFAULTS, environ=environ)


def apply_summary_yahoo_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(SUMMARY_YAHOO_DEFAULTS, environ=environ)


def apply_site_defaults(*, context: str = "", environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Compatibility wrapper used by runtime_env_defaults_patch.py/sitecustomize.py."""
    return _merge_applied(
        apply_push_defaults(environ=environ),
        apply_rescue_defaults(environ=environ),
        apply_db_defaults(environ=environ),
        apply_helper_defaults(environ=environ),
        apply_ranking_entry_defaults(environ=environ),
        apply_tonosama_defaults(environ=environ),
        apply_entry_defaults(environ=environ),
        apply_summary_yahoo_defaults(environ=environ),
    )


def apply_user_defaults(*, context: str = "", environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Compatibility wrapper used by runtime_env_defaults_patch.py/usercustomize.py."""
    return _merge_applied(
        apply_main_restore_defaults(environ=environ),
        apply_ranking_entry_defaults(environ=environ),
        apply_tonosama_defaults(environ=environ),
        apply_entry_defaults(environ=environ),
        apply_summary_yahoo_defaults(environ=environ),
    )


__all__ = [
    "VERSION",
    "env_bool",
    "apply_push_defaults",
    "apply_rescue_defaults",
    "apply_db_defaults",
    "apply_helper_defaults",
    "apply_main_restore_defaults",
    "apply_ranking_entry_defaults",
    "apply_tonosama_defaults",
    "apply_entry_defaults",
    "apply_summary_yahoo_defaults",
    "apply_site_defaults",
    "apply_user_defaults",
]
