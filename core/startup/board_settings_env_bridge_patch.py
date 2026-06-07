# ============================================================
# File   : core/startup/board_settings_env_bridge_patch.py
# Version: V1.3-SETTINGS-INI-TO-ENV-BRIDGE-SELF-CHECK-PATH
# ------------------------------------------------------------
# RESTフル板/返済指値/未約定管理のruntime patchは os.getenv() を読む。
# そのため settings.ini に書いた値を起動時に os.environ へ反映する。
#
# 優先順位:
#   1. 既存の環境変数があれば上書きしない
#   2. settings.ini の [board_runtime] / [entry] / [exit] / [DEFAULT]
#   3. このファイル内の安全デフォルト
# ============================================================

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
_INSTALLED = False

_DEFAULTS = {
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

_SECTIONS = ("board_runtime", "entry", "exit", "DEFAULT")


def _read_settings() -> configparser.ConfigParser:
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


def _lookup(conf: configparser.ConfigParser, key: str) -> str | None:
    # そのままの大文字キーと小文字キーの両方を許容する。
    keys = [key, key.lower()]
    for sec in _SECTIONS:
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


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    conf = _read_settings()
    applied = 0
    kept = 0
    for key, default in _DEFAULTS.items():
        if key in os.environ and str(os.environ.get(key, "")).strip() != "":
            kept += 1
            continue
        value = _lookup(conf, key)
        if value is None or value == "":
            value = default
        os.environ[key] = str(value)
        applied += 1
    _INSTALLED = True
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


try:
    install()
except Exception:
    logger.exception("[BOARD SETTINGS ENV] auto install failed")


__all__ = ["install"]
