# ============================================================
# File   : core/startup/startup.py
# Version: FINAL-PRODUCTION-REV25.2-DIRECT-REST-BOARD-FALLBACK
# ------------------------------------------------------------
# 【概要】
#   system_startup の公開入口
#
# 【設計】
#   - このファイルは起動入口だけ
#   - 実際の起動順序は startup_orchestrator.py に委譲
#   - 詳細処理は push_startup / scheduler_startup / summary_startup 等へ分離
#
# REV25.2:
#   - final_entry_board_rest_direct_patch を起動時に明示適用
#   - PUSHローテ外銘柄で final_entry_safety_guard が board_missing になり
#     発注直前で止まる問題を、kabu Station REST /board fallback で抑止
# ============================================================

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path

from core.startup.startup_orchestrator import run_system_startup

logger = logging.getLogger(__name__)


# ============================================================
# board settings -> env bridge
# (旧 core/startup/board_settings_env_bridge_patch.py V1.3 から移設)
# ------------------------------------------------------------
# RESTフル板/返済指値/未約定管理のruntime patchは os.getenv() を読む。
# そのため settings.ini に書いた値を起動時に os.environ へ反映する。
#
# 優先順位:
#   1. 既存の環境変数があれば上書きしない
#   2. settings.ini の [board_runtime] / [entry] / [exit] / [DEFAULT]
#   3. このファイル内の安全デフォルト
# ============================================================
_BOARD_SETTINGS_ENV_DEFAULTS = {
    # Entry REST full board
    "ENTRY_REST_FULL_BOARD_ENABLED": "1",
    "ENTRY_REST_FULL_BOARD_SOURCES": "SUMMARY_AI,RANKING,TONOSAMA,EARLY_SCALP,ENTRY",
    "ENTRY_REST_FULL_BOARD_EXCHANGE": "1",
    "ENTRY_REST_FULL_BOARD_DEPTH": "10",
    "ENTRY_REST_FULL_BOARD_THICK_MIN_QTY": "500",
    "ENTRY_REST_FULL_BOARD_MAX_SPREAD_PCT": "0.15",
    "ENTRY_REST_FULL_BOARD_STRICT_GUARD": "1",
    "ENTRY_REST_FULL_BOARD_CACHE_SEC": "0.8",
    "ENTRY_REST_FULL_BOARD_MIN_INTERVAL_SEC": "0.7",
    "ENTRY_REST_FULL_BOARD_TIMEOUT_SEC": "0.8",
    "ENTRY_REST_FULL_BOARD_IMBALANCE_GUARD_ENABLED": "1",
    "ENTRY_REST_FULL_BOARD_IMBALANCE_STRICT": "1",
    "ENTRY_REST_FULL_BOARD_IMBALANCE_DEPTH": "5",
    "ENTRY_REST_FULL_BOARD_MIN_SAME_SIDE_TOTAL": "300",
    "ENTRY_REST_FULL_BOARD_MAX_OPPOSITE_RATIO": "2.5",
    "ENTRY_REST_FULL_BOARD_RATIO_MIN_DENOM": "100",
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_ENABLED": "1",
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_STRICT": "1",
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_WAIT_SEC": "0.25",
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_MIN_REMAIN_RATIO": "0.60",
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_FAIL_OPEN": "1",
    "ENTRY_REST_REPRICE_RETRY_ONCE": "1",
    "ENTRY_SUMMARY_RETRY_MAX_ROUNDS": "1",
    "ENTRY_SUMMARY_RETRY_SYMBOL_COOLDOWN_SEC": "4.0",

    # Exit REST full board / pending close
    "EXIT_REST_FULL_BOARD_ENABLED": "1",
    "EXIT_REST_FULL_BOARD_EXCHANGE": "1",
    "EXIT_REST_FULL_BOARD_DEPTH": "10",
    "EXIT_REST_FULL_BOARD_THICK_MIN_QTY": "500",
    "EXIT_REST_FULL_BOARD_CACHE_SEC": "0.8",
    "EXIT_REST_FULL_BOARD_MIN_INTERVAL_SEC": "0.7",
    "EXIT_REST_FULL_BOARD_TIMEOUT_SEC": "0.8",
    "EXIT_REST_FULL_BOARD_MAX_SPREAD_PCT": "0.15",
    "EXIT_REST_FULL_BOARD_STRICT_SPREAD": "0",
    "EXIT_REST_FULL_BOARD_MAX_TICKS_AWAY": "0",
    "EXIT_LIMIT_BOARD_TOUCH_ENABLED": "1",
    "EXIT_LIMIT_FALLBACK_MARKET_IF_NO_BOARD": "1",
    "EXIT_LIMIT_PENDING_CLOSE_ENABLED": "1",
    "EXIT_MARK_CLOSED_ON_ORDER_ACCEPT": "0",

    # Exit unfilled / fill confirm / stale reconcile
    "EXIT_UNFILLED_REPRICE_ENABLED": "1",
    "EXIT_UNFILLED_CANCEL_SEC": "1.2",
    "EXIT_UNFILLED_CHECK_INTERVAL_SEC": "0.5",
    "EXIT_UNFILLED_REPRICE_MAX_ROUNDS": "1",
    "EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL": "1",
    "EXIT_FILL_CONFIRM_ENABLED": "1",
    "EXIT_FILL_CONFIRM_INTERVAL_SEC": "1.0",
    "EXIT_CLOSING_RECONCILE_ENABLED": "1",
    "EXIT_CLOSING_STALE_SEC": "20",
    "EXIT_CLOSING_RECONCILE_INTERVAL_SEC": "5",
    "EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC": "1.5",
    "EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK": "0",

    # REST API monitor
    "BOARD_REST_API_MONITOR_ENABLED": "1",
    "BOARD_REST_API_MONITOR_INTERVAL_SEC": "60",
    "BOARD_REST_API_MONITOR_WARN_BOARD_PER_MIN": "120",

    # Self check
    "BOARD_RUNTIME_SELF_CHECK_PATH": "runtime/diagnostics/board_runtime_self_check.json",
}

_BOARD_SETTINGS_ENV_SECTIONS = ("board_runtime", "entry", "exit", "DEFAULT")
_board_settings_env_bridge_installed = False


def _read_board_settings_ini() -> configparser.ConfigParser:
    conf = configparser.ConfigParser()
    paths = [
        Path("settings.ini"),
        Path("settings.local.ini"),
    ]
    for p in paths:
        try:
            if p.exists():
                conf.read(str(p), encoding="utf-8")
        except Exception:
            logger.debug("[BOARD SETTINGS ENV] failed to read %s", p, exc_info=True)
    return conf


def _lookup_board_settings_ini(conf: configparser.ConfigParser, key: str) -> str | None:
    # そのままの大文字キーと小文字キーの両方を許容する。
    keys = [key, key.lower()]
    for sec in _BOARD_SETTINGS_ENV_SECTIONS:
        try:
            if sec == "DEFAULT":
                source = conf.defaults()
                for k in keys:
                    if k in source:
                        return str(source[k]).strip()
                continue
            if not conf.has_section(sec):
                continue
            for k in keys:
                if conf.has_option(sec, k):
                    return str(conf.get(sec, k)).strip()
        except Exception:
            continue
    return None


def install_board_settings_env_bridge() -> bool:
    global _board_settings_env_bridge_installed
    if _board_settings_env_bridge_installed:
        return True
    conf = _read_board_settings_ini()
    applied = 0
    kept = 0
    for key, default in _BOARD_SETTINGS_ENV_DEFAULTS.items():
        if key in os.environ and str(os.environ.get(key, "")).strip() != "":
            kept += 1
            continue
        value = _lookup_board_settings_ini(conf, key)
        if value is None or value == "":
            value = default
        os.environ[key] = str(value)
        applied += 1
    _board_settings_env_bridge_installed = True
    logger.warning(
        "[BOARD SETTINGS ENV] installed applied=%s kept_existing_env=%s entry_rest=%s exit_rest=%s exit_reconcile=%s reconcile_rest_timeout=%s memory_fallback=%s api_monitor=%s self_check_path=%s",
        applied,
        kept,
        os.environ.get("ENTRY_REST_FULL_BOARD_ENABLED"),
        os.environ.get("EXIT_REST_FULL_BOARD_ENABLED"),
        os.environ.get("EXIT_CLOSING_RECONCILE_ENABLED"),
        os.environ.get("EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC"),
        os.environ.get("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK"),
        os.environ.get("BOARD_REST_API_MONITOR_ENABLED"),
        os.environ.get("BOARD_RUNTIME_SELF_CHECK_PATH"),
    )
    return True


# 起動時にRESTフル板/未約定/返済CLOSING関連の実効設定を1回だけまとめてログ出力する。
# 旧 core/startup/board_runtime_diagnostics_patch.py から移設。
_BOARD_DIAG_GROUPS = {
    "ENTRY_REST": [
        "ENTRY_REST_FULL_BOARD_ENABLED",
        "ENTRY_REST_FULL_BOARD_SOURCES",
        "ENTRY_REST_FULL_BOARD_EXCHANGE",
        "ENTRY_REST_FULL_BOARD_DEPTH",
        "ENTRY_REST_FULL_BOARD_THICK_MIN_QTY",
        "ENTRY_REST_FULL_BOARD_MAX_SPREAD_PCT",
        "ENTRY_REST_FULL_BOARD_STRICT_GUARD",
        "ENTRY_REST_FULL_BOARD_CACHE_SEC",
        "ENTRY_REST_FULL_BOARD_MIN_INTERVAL_SEC",
        "ENTRY_REST_FULL_BOARD_TIMEOUT_SEC",
    ],
    "ENTRY_IMBALANCE": [
        "ENTRY_REST_FULL_BOARD_IMBALANCE_GUARD_ENABLED",
        "ENTRY_REST_FULL_BOARD_IMBALANCE_STRICT",
        "ENTRY_REST_FULL_BOARD_IMBALANCE_DEPTH",
        "ENTRY_REST_FULL_BOARD_MIN_SAME_SIDE_TOTAL",
        "ENTRY_REST_FULL_BOARD_MAX_OPPOSITE_RATIO",
        "ENTRY_REST_FULL_BOARD_RATIO_MIN_DENOM",
    ],
    "ENTRY_DOUBLE_CHECK_RETRY": [
        "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_ENABLED",
        "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_STRICT",
        "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_WAIT_SEC",
        "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_MIN_REMAIN_RATIO",
        "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_FAIL_OPEN",
        "ENTRY_REST_REPRICE_RETRY_ONCE",
        "ENTRY_SUMMARY_RETRY_MAX_ROUNDS",
        "ENTRY_SUMMARY_RETRY_SYMBOL_COOLDOWN_SEC",
    ],
    "EXIT_REST": [
        "EXIT_REST_FULL_BOARD_ENABLED",
        "EXIT_REST_FULL_BOARD_EXCHANGE",
        "EXIT_REST_FULL_BOARD_DEPTH",
        "EXIT_REST_FULL_BOARD_THICK_MIN_QTY",
        "EXIT_REST_FULL_BOARD_CACHE_SEC",
        "EXIT_REST_FULL_BOARD_MIN_INTERVAL_SEC",
        "EXIT_REST_FULL_BOARD_TIMEOUT_SEC",
        "EXIT_REST_FULL_BOARD_MAX_SPREAD_PCT",
        "EXIT_REST_FULL_BOARD_STRICT_SPREAD",
        "EXIT_REST_FULL_BOARD_MAX_TICKS_AWAY",
        "EXIT_LIMIT_BOARD_TOUCH_ENABLED",
        "EXIT_LIMIT_FALLBACK_MARKET_IF_NO_BOARD",
    ],
    "EXIT_UNFILLED_CLOSING": [
        "EXIT_LIMIT_PENDING_CLOSE_ENABLED",
        "EXIT_MARK_CLOSED_ON_ORDER_ACCEPT",
        "EXIT_UNFILLED_REPRICE_ENABLED",
        "EXIT_UNFILLED_CANCEL_SEC",
        "EXIT_UNFILLED_CHECK_INTERVAL_SEC",
        "EXIT_UNFILLED_REPRICE_MAX_ROUNDS",
        "EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL",
        "EXIT_FILL_CONFIRM_ENABLED",
        "EXIT_FILL_CONFIRM_INTERVAL_SEC",
        "EXIT_CLOSING_RECONCILE_ENABLED",
        "EXIT_CLOSING_STALE_SEC",
        "EXIT_CLOSING_RECONCILE_INTERVAL_SEC",
        "EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC",
        "EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK",
    ],
    "API_MONITOR": [
        "BOARD_REST_API_MONITOR_ENABLED",
        "BOARD_REST_API_MONITOR_INTERVAL_SEC",
        "BOARD_REST_API_MONITOR_WARN_BOARD_PER_MIN",
    ],
    "SELF_CHECK": [
        "BOARD_RUNTIME_SELF_CHECK_PATH",
    ],
}

_board_diag_installed = False


def _board_diag_v(key: str) -> str:
    val = os.environ.get(key)
    if val is None or str(val).strip() == "":
        return "<unset>"
    return str(val).strip()


def _board_diag_bool_on(key: str) -> bool:
    return _board_diag_v(key).lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _board_diag_summary_line(name: str, keys: list[str]) -> str:
    return f"[BOARD RUNTIME DIAG] {name} " + " ".join(f"{k}={_board_diag_v(k)}" for k in keys)


def _log_board_runtime_diagnostics() -> bool:
    global _board_diag_installed
    if _board_diag_installed:
        return True
    for name, keys in _BOARD_DIAG_GROUPS.items():
        logger.warning(_board_diag_summary_line(name, keys))

    logger.warning(
        "[BOARD RUNTIME DIAG] EFFECTIVE entry_rest=%s entry_imbalance=%s entry_double_check=%s exit_rest=%s exit_pending_close=%s exit_reprice=%s exit_fill_confirm=%s exit_stale_reconcile=%s reconcile_memory_fallback=%s api_monitor=%s self_check_path=%s",
        _board_diag_bool_on("ENTRY_REST_FULL_BOARD_ENABLED"),
        _board_diag_bool_on("ENTRY_REST_FULL_BOARD_IMBALANCE_GUARD_ENABLED"),
        _board_diag_bool_on("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_ENABLED"),
        _board_diag_bool_on("EXIT_REST_FULL_BOARD_ENABLED"),
        _board_diag_bool_on("EXIT_LIMIT_PENDING_CLOSE_ENABLED"),
        _board_diag_bool_on("EXIT_UNFILLED_REPRICE_ENABLED"),
        _board_diag_bool_on("EXIT_FILL_CONFIRM_ENABLED"),
        _board_diag_bool_on("EXIT_CLOSING_RECONCILE_ENABLED"),
        _board_diag_bool_on("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK"),
        _board_diag_bool_on("BOARD_REST_API_MONITOR_ENABLED"),
        _board_diag_v("BOARD_RUNTIME_SELF_CHECK_PATH"),
    )
    _board_diag_installed = True
    return True


def _install_entrypoint_runtime_patches() -> None:
    try:
        install_board_settings_env_bridge()
    except Exception:
        logger.exception("[startup.entrypoint] board settings env bridge install failed")

    try:
        from core.startup.board_runtime_safety_clamp_patch import install as install_board_runtime_safety_clamp

        install_board_runtime_safety_clamp()
    except Exception:
        logger.exception("[startup.entrypoint] board runtime safety clamp install failed")

    try:
        _log_board_runtime_diagnostics()
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

    # entry_pipeline_pending_root_prefilter_patch は
    # trading/handlers/entry_controller.py へインライン化済み
    # (パッチ側は空スタブだったので削除した)。

    try:
        from core.startup.entry_volume_direction_guard_patch import install as install_entry_volume_direction_guard

        install_entry_volume_direction_guard()
    except Exception:
        logger.exception("[startup.entrypoint] entry volume direction guard install failed")

    # ranking_entry_fast_runtime_patch は trading/ranking/entry_from_ranking.py
    # (_light_prefilter_rows) と trading/ranking/ranking_technical_store.py
    # (readonly/memory-cache 版 attach/save) へ本文化済みのため install 呼び出しを削除した。

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

    # entry_execute_timeout_guard_patch は
    # trading/handlers/entry_controller._execute_best_candidate 本体へ
    # インライン化済み (パッチ側は空スタブだったので削除した)。

    try:
        from core.startup.rest_full_board_entry_patch import install as install_rest_full_board_entry_patch

        install_rest_full_board_entry_patch()
    except Exception:
        logger.exception("[startup.entrypoint] REST full board entry patch install failed")

    # final_entry_safety_guard_patch は trading/handlers/entry_controller.py の
    # _final_guard_* 群 (時間帯/流動性/直近反転/板) へインライン化済み
    # (パッチ側は空スタブだったので削除した)。

    try:
        from core.startup.final_entry_board_rest_direct_patch import install as install_final_entry_board_rest_direct_patch

        install_final_entry_board_rest_direct_patch()
    except Exception:
        logger.exception("[startup.entrypoint] final entry direct REST board fallback install failed")

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
    logger.info("🚀 system_startup entry REV25.2-DIRECT-REST-BOARD-FALLBACK")
    _install_entrypoint_runtime_patches()
    return run_system_startup()


__all__ = [
    "system_startup",
]
