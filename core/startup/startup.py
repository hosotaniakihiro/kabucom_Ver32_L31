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

import collections
import configparser
import datetime as dt
import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

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


# ============================================================
# board runtime safety clamp
# (旧 core/startup/board_runtime_safety_clamp_patch.py V1.2 から移設)
# ------------------------------------------------------------
# settings.ini / env に危険な値が入っても、板関連runtime設定を
# 起動時に安全範囲へ補正する。
#
# 目的:
#   - REST API連打を防ぐ
#   - 返済取消が短すぎて無駄キャンセル連発になるのを防ぐ
#   - 板バランスガードが厳しすぎ/緩すぎになるのを防ぐ
#   - CLOSING救済が早すぎて誤判定するのを防ぐ
# ============================================================
_BOARD_SAFETY_CLAMP_FLOAT_RANGES = {
    # REST API call pressure
    "ENTRY_REST_FULL_BOARD_CACHE_SEC": (0.2, 5.0, 0.8),
    "ENTRY_REST_FULL_BOARD_MIN_INTERVAL_SEC": (0.3, 5.0, 0.7),
    "ENTRY_REST_FULL_BOARD_TIMEOUT_SEC": (0.3, 3.0, 0.8),
    "EXIT_REST_FULL_BOARD_CACHE_SEC": (0.2, 5.0, 0.8),
    "EXIT_REST_FULL_BOARD_MIN_INTERVAL_SEC": (0.3, 5.0, 0.7),
    "EXIT_REST_FULL_BOARD_TIMEOUT_SEC": (0.3, 3.0, 0.8),

    # Board guards
    "ENTRY_REST_FULL_BOARD_THICK_MIN_QTY": (100.0, 20000.0, 500.0),
    "EXIT_REST_FULL_BOARD_THICK_MIN_QTY": (100.0, 20000.0, 500.0),
    "ENTRY_REST_FULL_BOARD_MAX_SPREAD_PCT": (0.02, 1.0, 0.15),
    "EXIT_REST_FULL_BOARD_MAX_SPREAD_PCT": (0.02, 2.0, 0.15),
    "ENTRY_REST_FULL_BOARD_MIN_SAME_SIDE_TOTAL": (100.0, 100000.0, 300.0),
    "ENTRY_REST_FULL_BOARD_MAX_OPPOSITE_RATIO": (1.2, 20.0, 2.5),
    "ENTRY_REST_FULL_BOARD_RATIO_MIN_DENOM": (1.0, 10000.0, 100.0),

    # Double check
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_WAIT_SEC": (0.05, 1.5, 0.25),
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_MIN_REMAIN_RATIO": (0.2, 0.95, 0.60),

    # Entry unfilled retry
    "ENTRY_SUMMARY_RETRY_SYMBOL_COOLDOWN_SEC": (1.0, 30.0, 4.0),

    # Exit unfilled / closing
    "EXIT_UNFILLED_CANCEL_SEC": (0.8, 10.0, 1.2),
    "EXIT_UNFILLED_CHECK_INTERVAL_SEC": (0.2, 5.0, 0.5),
    "EXIT_FILL_CONFIRM_INTERVAL_SEC": (0.5, 10.0, 1.0),
    "EXIT_CLOSING_STALE_SEC": (5.0, 300.0, 20.0),
    "EXIT_CLOSING_RECONCILE_INTERVAL_SEC": (1.0, 60.0, 5.0),
    "EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC": (0.5, 5.0, 1.5),

    # API monitor
    "BOARD_REST_API_MONITOR_INTERVAL_SEC": (10.0, 300.0, 60.0),
    "BOARD_REST_API_MONITOR_WARN_BOARD_PER_MIN": (10.0, 2000.0, 120.0),
}

_BOARD_SAFETY_CLAMP_INT_RANGES = {
    "ENTRY_REST_FULL_BOARD_DEPTH": (1, 10, 10),
    "EXIT_REST_FULL_BOARD_DEPTH": (1, 10, 10),
    "ENTRY_REST_FULL_BOARD_IMBALANCE_DEPTH": (1, 10, 5),
    "ENTRY_SUMMARY_RETRY_MAX_ROUNDS": (0, 3, 1),
    "EXIT_UNFILLED_REPRICE_MAX_ROUNDS": (0, 3, 1),
    "EXIT_REST_FULL_BOARD_MAX_TICKS_AWAY": (0, 5, 0),
}

_BOARD_SAFETY_CLAMP_BOOL_KEYS = [
    "ENTRY_REST_FULL_BOARD_ENABLED",
    "ENTRY_REST_FULL_BOARD_STRICT_GUARD",
    "ENTRY_REST_FULL_BOARD_IMBALANCE_GUARD_ENABLED",
    "ENTRY_REST_FULL_BOARD_IMBALANCE_STRICT",
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_ENABLED",
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_STRICT",
    "ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_FAIL_OPEN",
    "ENTRY_REST_REPRICE_RETRY_ONCE",
    "EXIT_REST_FULL_BOARD_ENABLED",
    "EXIT_REST_FULL_BOARD_STRICT_SPREAD",
    "EXIT_LIMIT_BOARD_TOUCH_ENABLED",
    "EXIT_LIMIT_FALLBACK_MARKET_IF_NO_BOARD",
    "EXIT_LIMIT_PENDING_CLOSE_ENABLED",
    "EXIT_MARK_CLOSED_ON_ORDER_ACCEPT",
    "EXIT_UNFILLED_REPRICE_ENABLED",
    "EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL",
    "EXIT_FILL_CONFIRM_ENABLED",
    "EXIT_CLOSING_RECONCILE_ENABLED",
    "EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK",
    "BOARD_REST_API_MONITOR_ENABLED",
]

_BOARD_SAFETY_CLAMP_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled", "ok"}
_BOARD_SAFETY_CLAMP_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled", "ng", ""}
_board_runtime_safety_clamp_installed = False


def _board_safety_clamp_float(key: str, lo: float, hi: float, default: float) -> bool:
    raw = os.environ.get(key)
    changed = False
    try:
        val = float(raw) if raw not in (None, "") else float(default)
    except Exception:
        val = float(default)
        changed = True
    if val < lo:
        val = lo
        changed = True
    elif val > hi:
        val = hi
        changed = True
    new = str(val)
    if str(raw) != new:
        os.environ[key] = new
        changed = True
    return changed


def _board_safety_clamp_int(key: str, lo: int, hi: int, default: int) -> bool:
    raw = os.environ.get(key)
    changed = False
    try:
        val = int(float(raw)) if raw not in (None, "") else int(default)
    except Exception:
        val = int(default)
        changed = True
    if val < lo:
        val = lo
        changed = True
    elif val > hi:
        val = hi
        changed = True
    new = str(val)
    if str(raw) != new:
        os.environ[key] = new
        changed = True
    return changed


def _board_safety_clamp_normalize_bool(key: str) -> bool:
    raw = os.environ.get(key)
    s = str(raw or "").strip().lower()
    if s in _BOARD_SAFETY_CLAMP_TRUE:
        new = "1"
    elif s in _BOARD_SAFETY_CLAMP_FALSE:
        new = "0"
    else:
        new = "1" if bool(raw) else "0"
    if str(raw) != new:
        os.environ[key] = new
        return True
    return False


def _board_safety_clamp_dependency_clamps() -> list[str]:
    notes: list[str] = []
    # EXIT板を使わない場合、指値タッチも無効化する。
    if os.environ.get("EXIT_REST_FULL_BOARD_ENABLED") == "0" and os.environ.get("EXIT_LIMIT_BOARD_TOUCH_ENABLED") == "1":
        os.environ["EXIT_LIMIT_BOARD_TOUCH_ENABLED"] = "0"
        notes.append("EXIT_LIMIT_BOARD_TOUCH_ENABLED=0 because EXIT_REST_FULL_BOARD_ENABLED=0")

    # 未約定再発注を無効化する場合、最終成行fallbackも無効化する。
    if os.environ.get("EXIT_UNFILLED_REPRICE_ENABLED") == "0" and os.environ.get("EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL") == "1":
        os.environ["EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL"] = "0"
        notes.append("EXIT_UNFILLED_REPRICE_MARKET_ON_FINAL=0 because EXIT_UNFILLED_REPRICE_ENABLED=0")

    # CLOSING救済はfill confirmより遅くする。
    try:
        stale = float(os.environ.get("EXIT_CLOSING_STALE_SEC", "20"))
        cancel = float(os.environ.get("EXIT_UNFILLED_CANCEL_SEC", "1.2"))
        min_stale = max(5.0, cancel + 3.0)
        if stale < min_stale:
            os.environ["EXIT_CLOSING_STALE_SEC"] = str(min_stale)
            notes.append(f"EXIT_CLOSING_STALE_SEC={min_stale} because cancel_sec={cancel}")
    except Exception:
        pass
    return notes


def install_board_runtime_safety_clamp() -> bool:
    global _board_runtime_safety_clamp_installed
    if _board_runtime_safety_clamp_installed:
        return True
    changed_keys: list[str] = []
    for key, (lo, hi, default) in _BOARD_SAFETY_CLAMP_FLOAT_RANGES.items():
        if _board_safety_clamp_float(key, lo, hi, default):
            changed_keys.append(key)
    for key, (lo, hi, default) in _BOARD_SAFETY_CLAMP_INT_RANGES.items():
        if _board_safety_clamp_int(key, lo, hi, default):
            changed_keys.append(key)
    for key in _BOARD_SAFETY_CLAMP_BOOL_KEYS:
        if _board_safety_clamp_normalize_bool(key):
            changed_keys.append(key)
    notes = _board_safety_clamp_dependency_clamps()
    _board_runtime_safety_clamp_installed = True
    logger.warning(
        "[BOARD RUNTIME SAFETY] installed changed=%s keys=%s notes=%s reconcile_rest_timeout=%s memory_fallback=%s api_monitor=%s monitor_interval=%s warn_board_per_min=%s",
        len(changed_keys),
        changed_keys[:40],
        notes,
        os.environ.get("EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC"),
        os.environ.get("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK"),
        os.environ.get("BOARD_REST_API_MONITOR_ENABLED"),
        os.environ.get("BOARD_REST_API_MONITOR_INTERVAL_SEC"),
        os.environ.get("BOARD_REST_API_MONITOR_WARN_BOARD_PER_MIN"),
    )
    return True


# ============================================================
# board REST API monitor
# (旧 core/startup/board_rest_api_monitor_patch.py V1 から移設)
# ------------------------------------------------------------
# RESTフル板導入後のAPI呼び出し状況を監視する。
# urllib.request.urlopen を軽くwrapし、kabusapi /board /positions の
# 呼び出し回数・失敗回数・平均時間を1分ごとにログ出力する。
#
# 注意:
#   - 挙動は変えない。監視だけ。
#   - URLがkabusapi以外なら集計しない。
# ============================================================
_board_rest_api_monitor_installed = False
_board_rest_api_monitor_orig_urlopen = None
_board_rest_api_monitor_lock = threading.RLock()
_board_rest_api_monitor_counters: dict[str, dict[str, float]] = collections.defaultdict(
    lambda: {"ok": 0.0, "ng": 0.0, "total_ms": 0.0, "max_ms": 0.0}
)
_board_rest_api_monitor_recent: collections.deque = collections.deque(maxlen=2000)


def _board_rest_api_monitor_env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _board_rest_api_monitor_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _board_rest_api_monitor_url_of(req: Any) -> str:
    try:
        if isinstance(req, str):
            return req
        if hasattr(req, "full_url"):
            return str(req.full_url)
        if hasattr(req, "get_full_url"):
            return str(req.get_full_url())
    except Exception:
        pass
    return ""


def _board_rest_api_monitor_kind(url: str) -> str | None:
    u = str(url or "").lower()
    if "kabusapi" not in u:
        return None
    if "/board/" in u or u.endswith("/board"):
        return "board"
    if "/positions" in u:
        return "positions"
    if "/orders" in u:
        return "orders"
    return None


def _board_rest_api_monitor_record(kind: str, ok: bool, elapsed_ms: float) -> None:
    now = time.time()
    with _board_rest_api_monitor_lock:
        d = _board_rest_api_monitor_counters[kind]
        if ok:
            d["ok"] += 1
        else:
            d["ng"] += 1
        d["total_ms"] += float(elapsed_ms)
        d["max_ms"] = max(float(d.get("max_ms", 0.0)), float(elapsed_ms))
        _board_rest_api_monitor_recent.append((now, kind, ok, float(elapsed_ms)))


def _board_rest_api_monitor_snapshot_window(sec: float) -> dict[str, dict[str, float]]:
    cutoff = time.time() - sec
    out: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: {"ok": 0.0, "ng": 0.0, "total_ms": 0.0, "max_ms": 0.0}
    )
    with _board_rest_api_monitor_lock:
        for ts, kind, ok, ms in list(_board_rest_api_monitor_recent):
            if ts < cutoff:
                continue
            d = out[kind]
            if ok:
                d["ok"] += 1
            else:
                d["ng"] += 1
            d["total_ms"] += ms
            d["max_ms"] = max(d["max_ms"], ms)
    return dict(out)


def _board_rest_api_monitor_format_stats(stats: dict[str, dict[str, float]]) -> str:
    parts = []
    for kind in ("board", "positions", "orders"):
        d = stats.get(kind, {})
        ok = int(d.get("ok", 0))
        ng = int(d.get("ng", 0))
        total = ok + ng
        avg = (d.get("total_ms", 0.0) / total) if total else 0.0
        mx = d.get("max_ms", 0.0)
        parts.append(f"{kind}:total={total} ok={ok} ng={ng} avg_ms={avg:.1f} max_ms={mx:.1f}")
    return " | ".join(parts)


def _board_rest_api_monitor_report_loop() -> None:
    interval = max(10.0, _board_rest_api_monitor_env_float("BOARD_REST_API_MONITOR_INTERVAL_SEC", 60.0))
    warn_board_per_min = max(1.0, _board_rest_api_monitor_env_float("BOARD_REST_API_MONITOR_WARN_BOARD_PER_MIN", 120.0))
    while True:
        try:
            stats = _board_rest_api_monitor_snapshot_window(interval)
            line = _board_rest_api_monitor_format_stats(stats)
            board_total = int(stats.get("board", {}).get("ok", 0) + stats.get("board", {}).get("ng", 0))
            if board_total >= warn_board_per_min:
                logger.warning(
                    "[BOARD REST API MONITOR] HIGH_USAGE window=%.0fs %s warn_board_per_min=%.0f",
                    interval, line, warn_board_per_min,
                )
            else:
                logger.info("[BOARD REST API MONITOR] window=%.0fs %s", interval, line)
        except Exception:
            logger.exception("[BOARD REST API MONITOR] report loop error")
        time.sleep(interval)


def install_board_rest_api_monitor() -> bool:
    global _board_rest_api_monitor_installed, _board_rest_api_monitor_orig_urlopen
    if _board_rest_api_monitor_installed:
        return True
    if not _board_rest_api_monitor_env_bool("BOARD_REST_API_MONITOR_ENABLED", True):
        logger.warning("[BOARD REST API MONITOR] disabled")
        return False
    old = urllib.request.urlopen
    if getattr(old, "_board_rest_api_monitor_wrapped", False):
        _board_rest_api_monitor_installed = True
        return True
    _board_rest_api_monitor_orig_urlopen = old

    def wrapped_urlopen(req, *args, **kwargs):
        url = _board_rest_api_monitor_url_of(req)
        k = _board_rest_api_monitor_kind(url)
        if not k:
            return old(req, *args, **kwargs)
        start = time.perf_counter()
        try:
            ret = old(req, *args, **kwargs)
            _board_rest_api_monitor_record(k, True, (time.perf_counter() - start) * 1000.0)
            return ret
        except Exception:
            _board_rest_api_monitor_record(k, False, (time.perf_counter() - start) * 1000.0)
            raise

    wrapped_urlopen._board_rest_api_monitor_wrapped = True  # type: ignore[attr-defined]
    wrapped_urlopen._original = old  # type: ignore[attr-defined]
    urllib.request.urlopen = wrapped_urlopen
    threading.Thread(target=_board_rest_api_monitor_report_loop, daemon=True, name="board_rest_api_monitor_loop").start()
    _board_rest_api_monitor_installed = True
    logger.warning(
        "[BOARD REST API MONITOR] installed interval=%.0fs warn_board_per_min=%.0f",
        _board_rest_api_monitor_env_float("BOARD_REST_API_MONITOR_INTERVAL_SEC", 60.0),
        _board_rest_api_monitor_env_float("BOARD_REST_API_MONITOR_WARN_BOARD_PER_MIN", 120.0),
    )
    return True


# ============================================================
# board runtime self check
# (旧 core/startup/board_runtime_self_check_patch.py V1.1 から移設)
# ------------------------------------------------------------
# マーケット時間外でも確認できる起動時セルフチェック。
# APIは叩かず、runtime patchが実際にwrap/起動されているかを確認する。
#
# V1.1:
#   - runtime/diagnostics/board_runtime_self_check.json に結果保存
#   - ログが流れても後から確認できるようにする
# ============================================================
_board_runtime_self_check_installed = False


def _board_runtime_self_check_b(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _board_runtime_self_check_has_attr(obj: Any, attr: str) -> bool:
    try:
        return bool(getattr(obj, attr, False))
    except Exception:
        return False


def _board_runtime_self_check_chain_has(fn: Any, attr: str, limit: int = 30) -> bool:
    seen: set[int] = set()
    cur = fn
    for _ in range(limit):
        if not callable(cur):
            return False
        ident = id(cur)
        if ident in seen:
            return False
        seen.add(ident)
        if _board_runtime_self_check_has_attr(cur, attr):
            return True
        cur = getattr(cur, "_original", None) or getattr(cur, "_rest_full_board_original", None)
    return False


def _board_runtime_self_check_entry_order_builder() -> dict:
    out = {"available": False, "rest_full_board_wrapped": False}
    try:
        import trading.handlers.entry_order_builder as eob
        fn = getattr(eob, "build_entry_order", None)
        out["available"] = callable(fn)
        out["rest_full_board_wrapped"] = _board_runtime_self_check_chain_has(fn, "_rest_full_board_patch") if callable(fn) else False
    except Exception as e:
        out["error"] = repr(e)
    return out


def _board_runtime_self_check_exit_executor() -> dict:
    out = {
        "available": False,
        "limit_board_touch_wrapped": False,
        "pending_close_wrapped": False,
    }
    try:
        import trading.exit.executor as ex
        close_payload = getattr(ex, "_build_kabu_close_payload", None)
        close_db = getattr(ex, "_close_db_position", None)
        out["available"] = callable(close_payload) or callable(close_db)
        if callable(close_payload):
            out["limit_board_touch_wrapped"] = (
                # 本文化済み (trading/exit/executor._apply_exit_limit_board_touch)
                _board_runtime_self_check_chain_has(close_payload, "_exit_limit_board_touch_builtin")
                or _board_runtime_self_check_chain_has(close_payload, "_exit_limit_board_touch_wrapped_v11")
                or _board_runtime_self_check_chain_has(close_payload, "_exit_limit_board_touch_wrapped")
            )
        out["pending_close_wrapped"] = _board_runtime_self_check_chain_has(close_db, "_exit_limit_pending_close_wrapped") if callable(close_db) else False
    except Exception as e:
        out["error"] = repr(e)
    return out


def _board_runtime_self_check_global_data_retry_hook() -> dict:
    out = {"add_entry_inflight_wrapped": False}
    try:
        from global_state import global_data
        fn = getattr(global_data, "add_entry_inflight", None)
        out["add_entry_inflight_wrapped"] = _board_runtime_self_check_chain_has(fn, "_summary_retry_add_wrapped") if callable(fn) else False
    except Exception as e:
        out["error"] = repr(e)
    return out


def _board_runtime_self_check_urlopen_monitor() -> dict:
    fn = urllib.request.urlopen
    return {
        "urlopen_wrapped": _board_runtime_self_check_has_attr(fn, "_board_rest_api_monitor_wrapped"),
        "monitor_enabled_env": _board_runtime_self_check_b("BOARD_REST_API_MONITOR_ENABLED"),
    }


def _board_runtime_self_check_diagnostics_path() -> Path:
    raw = os.getenv("BOARD_RUNTIME_SELF_CHECK_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path("runtime") / "diagnostics" / "board_runtime_self_check.json"


def _board_runtime_self_check_save_result(payload: dict) -> None:
    try:
        p = _board_runtime_self_check_diagnostics_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        logger.warning("[BOARD RUNTIME SELF CHECK] saved path=%s", p)
    except Exception:
        logger.exception("[BOARD RUNTIME SELF CHECK] save diagnostics failed")


def install_board_runtime_self_check() -> bool:
    global _board_runtime_self_check_installed
    if _board_runtime_self_check_installed:
        return True
    checks = {
        "entry_order_builder": _board_runtime_self_check_entry_order_builder(),
        "exit_executor": _board_runtime_self_check_exit_executor(),
        "summary_retry": _board_runtime_self_check_global_data_retry_hook(),
        "urlopen_monitor": _board_runtime_self_check_urlopen_monitor(),
        "env": {
            "entry_rest": _board_runtime_self_check_b("ENTRY_REST_FULL_BOARD_ENABLED"),
            "entry_double_check": _board_runtime_self_check_b("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_ENABLED"),
            "exit_rest": _board_runtime_self_check_b("EXIT_REST_FULL_BOARD_ENABLED"),
            "exit_reprice": _board_runtime_self_check_b("EXIT_UNFILLED_REPRICE_ENABLED"),
            "exit_fill_confirm": _board_runtime_self_check_b("EXIT_FILL_CONFIRM_ENABLED"),
            "exit_closing_reconcile": _board_runtime_self_check_b("EXIT_CLOSING_RECONCILE_ENABLED"),
            "api_monitor": _board_runtime_self_check_b("BOARD_REST_API_MONITOR_ENABLED"),
        },
    }
    ok = True
    try:
        ok = bool(checks["entry_order_builder"].get("rest_full_board_wrapped"))
        ok = ok and bool(checks["exit_executor"].get("limit_board_touch_wrapped"))
        ok = ok and bool(checks["exit_executor"].get("pending_close_wrapped"))
        ok = ok and bool(checks["urlopen_monitor"].get("urlopen_wrapped"))
    except Exception:
        ok = False

    payload = {
        "ok": bool(ok),
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
        "diagnostics_path": str(_board_runtime_self_check_diagnostics_path()),
    }
    _board_runtime_self_check_save_result(payload)
    level = logger.warning if ok else logger.error
    level("[BOARD RUNTIME SELF CHECK] ok=%s checks=%s", ok, checks)
    _board_runtime_self_check_installed = True
    return ok


# ============================================================
# final entry board REST direct env defaults
# (旧 core/startup/final_entry_board_rest_direct_patch.py V4.1 から移設)
# ------------------------------------------------------------
# 目的:
#   SUMMARY_AI / TONOSAMA の発注直前に PUSH 板が無い場合に使われる
#   REST /board 直接フォールバック (trading/handlers/entry_controller の
#   _final_guard_try_get_bid_ask_from_api や core/startup/board_retry_patch.py
#   の _fetch_board_rest が読む ENTRY_BOARD_REST_DIRECT_* env) を、
#   起動時に強制的に有効化する。
#
# 注意:
#   - 旧パッチが提供していた _try_get_bid_ask_from_api という関数差し替え
#     ロジックは、差し替え先 (final_entry_safety_guard 側の同名関数) が
#     既に本文化されて存在しないため、旧パッチ自身のコード内コメントにも
#     ある通り事実上デッドコードだった (誰も呼んでいなかった)。
#     実効なのは下記の env 強制設定と、起動直後しばらく再適用するループのみ。
#   - ENTRY_BOARD_REST_DIRECT_ENABLED は setdefault ではなく毎回 "1" に
#     上書きする (旧パッチと同じ挙動)。DISABLE_ENTRY_BOARD_REST_DIRECT_PATCH=1
#     の場合のみ何もしない。
# ============================================================
_ENTRY_BOARD_REST_DIRECT_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_ENTRY_BOARD_REST_DIRECT_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}
_entry_board_rest_direct_watcher_started = False


def _entry_board_rest_direct_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    s = str(raw).strip().lower()
    if s in _ENTRY_BOARD_REST_DIRECT_TRUE:
        return True
    if s in _ENTRY_BOARD_REST_DIRECT_FALSE:
        return False
    return bool(default)


def _entry_board_rest_direct_env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _entry_board_rest_direct_env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(str(raw).replace(",", "")))
    except Exception:
        return int(default)


def _entry_board_rest_direct_patch_disabled() -> bool:
    return os.getenv("DISABLE_ENTRY_BOARD_REST_DIRECT_PATCH", "").strip() == "1"


def _set_entry_board_rest_direct_env_defaults() -> None:
    if _entry_board_rest_direct_patch_disabled():
        return
    # 旧バージョンが残した 0 を上書きする。明示停止は DISABLE_ENTRY_BOARD_REST_DIRECT_PATCH=1 に統一。
    os.environ["ENTRY_BOARD_REST_DIRECT_ENABLED"] = "1"
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", "0.6")
    os.environ.setdefault("ENTRY_BOARD_REST_EXCHANGES", "1")
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_LEVELS", "5")


def _install_entry_board_rest_direct_env_defaults_once(log_patch: bool = True) -> bool:
    try:
        _set_entry_board_rest_direct_env_defaults()
        if log_patch:
            logger.warning(
                "[FINAL ENTRY BOARD REST DIRECT] installed direct_enabled=%s disabled=%s timeout=%.3f hard_block=%s allow_without_board=%s",
                _entry_board_rest_direct_env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True),
                _entry_board_rest_direct_patch_disabled(),
                _entry_board_rest_direct_env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 0.6),
                _entry_board_rest_direct_env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True),
                _entry_board_rest_direct_env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False),
            )
        return True
    except Exception:
        logger.exception("[FINAL ENTRY BOARD REST DIRECT] install_once failed")
        return False


def _entry_board_rest_direct_watcher() -> None:
    loops = max(1, _entry_board_rest_direct_env_int("ENTRY_BOARD_REST_DIRECT_WATCH_LOOPS", 30))
    sleep_sec = max(0.5, _entry_board_rest_direct_env_float("ENTRY_BOARD_REST_DIRECT_WATCH_SLEEP_SEC", 1.0))
    for i in range(loops):
        ok = _install_entry_board_rest_direct_env_defaults_once(log_patch=False)
        if i in (0, loops - 1):
            logger.warning("[FINAL ENTRY BOARD REST DIRECT] enforce i=%s/%s ok=%s", i, loops, ok)
        time.sleep(sleep_sec)


def install_entry_board_rest_direct_env_defaults() -> bool:
    global _entry_board_rest_direct_watcher_started
    ok = _install_entry_board_rest_direct_env_defaults_once(log_patch=True)
    if not _entry_board_rest_direct_watcher_started:
        _entry_board_rest_direct_watcher_started = True
        threading.Thread(target=_entry_board_rest_direct_watcher, name="final-entry-board-rest-direct-enforcer", daemon=True).start()
    return bool(ok)


def _install_entrypoint_runtime_patches() -> None:
    try:
        install_board_settings_env_bridge()
    except Exception:
        logger.exception("[startup.entrypoint] board settings env bridge install failed")

    try:
        install_board_runtime_safety_clamp()
    except Exception:
        logger.exception("[startup.entrypoint] board runtime safety clamp install failed")

    try:
        _log_board_runtime_diagnostics()
    except Exception:
        logger.exception("[startup.entrypoint] board runtime diagnostics install failed")

    try:
        install_board_rest_api_monitor()
    except Exception:
        logger.exception("[startup.entrypoint] board REST API monitor install failed")

    try:
        from core.startup.tonosama_history_missing_guard_patch import install as install_tonosama_history_guard

        install_tonosama_history_guard()
    except Exception:
        logger.exception("[startup.entrypoint] tonosama history missing guard install failed")

    # summary_seed_recent_merged_guard_patch は core/startup/summary_db_seed_restore_patch.py の
    # _publish_interval (SUMMARY_SEED_RECENT_MERGED_GUARD_ENABLED gate 込み) へ native化済み。

    # daily_mtf_daily_src_alias_patch は削除した。
    # ラップ対象 trading/summary/mtf/daily_ma_mtf.attach_daily_ma_mtf_to_summary の
    # 読み手は3経路とも `from .daily_ma_mtf import ...` 形式で、しかもパッチ自身の
    # `import trading.summary.mtf.daily_ma_mtf` がパッケージ __init__ を先に走らせる
    # ため、install() が属性を差し替える時点で全ての束縛が既に元関数を掴んでいた。
    # モジュール属性経由で呼ぶ箇所は1つも無く、ラッパは一度も実行されていなかった。

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

    # entry_volume_direction_guard_patch は trading/handlers/entry_controller.py の
    # ai_final_entry_check (2層目 _vdg_*) へインライン化済みなので撤去した。

    # ranking_entry_fast_runtime_patch は trading/ranking/entry_from_ranking.py
    # (_light_prefilter_rows) と trading/ranking/ranking_technical_store.py
    # (readonly/memory-cache 版 attach/save) へ本文化済みのため install 呼び出しを削除した。

    # tonosama_fast_score_prefilter_patch は
    # trading/entry/tonosama/fast_score_prefilter.py へ本文化済み
    # (install() を持たないライブラリなので呼び出しは不要)。
    # 削除済みモジュールを import し続けており、起動のたびに
    # ImportError を except で拾って exception ログを出していたため除去した。

    # summary_entry_pending_duplicate_registered_patch は削除した (全体が死にコード)。
    # register_pending_entries を包むが、直後に install される
    # summary_ai_direct_timeout_continue_patch が `getattr(cur, "_original", cur)` で
    # existing_fix の wrapper を掴み直すため、このラッパは必ず捨てられていた。
    # 機能チェーン (_direct_add_pending / _protect_symbol 等) はすべてその
    # ラッパからしか呼ばれず、env 書き込みも他属性の差し替えも無い。

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
        install_entry_board_rest_direct_env_defaults()
    except Exception:
        logger.exception("[startup.entrypoint] final entry direct REST board fallback env defaults install failed")

    # exit_limit_pending_close_runtime_patch は trading/exit/executor.py の
    # _close_db_position (native化した _mark_db_position_closing 分岐) へ
    # インライン化済みのため install 呼び出しを削除した。

    # exit_unfilled_reprice_runtime_patch は trading/exit/executor.py の
    # start_exit_unfilled_reprice_loop() へ native化済み。
    try:
        from trading.exit.executor import start_exit_unfilled_reprice_loop

        start_exit_unfilled_reprice_loop()
    except Exception:
        logger.exception("[startup.entrypoint] exit unfilled reprice loop start failed")

    try:
        from trading.exit.executor import start_exit_fill_confirm_loop

        start_exit_fill_confirm_loop()
    except Exception:
        logger.exception("[startup.entrypoint] exit order fill confirm loop start failed")

    try:
        from trading.exit.executor import start_exit_closing_stale_reconcile_loop

        start_exit_closing_stale_reconcile_loop()
    except Exception:
        logger.exception("[startup.entrypoint] exit closing stale reconcile loop start failed")

    try:
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
