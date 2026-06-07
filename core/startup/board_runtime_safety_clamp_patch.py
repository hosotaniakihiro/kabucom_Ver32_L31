# ============================================================
# File   : core/startup/board_runtime_safety_clamp_patch.py
# Version: V1.1-BOARD-RUNTIME-SAFETY-CLAMP-RECONCILE-REST
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

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False

_FLOAT_RANGES = {
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
}

_INT_RANGES = {
    "ENTRY_REST_FULL_BOARD_DEPTH": (1, 10, 10),
    "EXIT_REST_FULL_BOARD_DEPTH": (1, 10, 10),
    "ENTRY_REST_FULL_BOARD_IMBALANCE_DEPTH": (1, 10, 5),
    "ENTRY_SUMMARY_RETRY_MAX_ROUNDS": (0, 3, 1),
    "EXIT_UNFILLED_REPRICE_MAX_ROUNDS": (0, 3, 1),
    "EXIT_REST_FULL_BOARD_MAX_TICKS_AWAY": (0, 5, 0),
}

_BOOL_KEYS = [
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
]

_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled", "ok"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled", "ng", ""}


def _clamp_float(key: str, lo: float, hi: float, default: float) -> bool:
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


def _clamp_int(key: str, lo: int, hi: int, default: int) -> bool:
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


def _normalize_bool(key: str) -> bool:
    raw = os.environ.get(key)
    s = str(raw or "").strip().lower()
    if s in _TRUE:
        new = "1"
    elif s in _FALSE:
        new = "0"
    else:
        new = "1" if bool(raw) else "0"
    if str(raw) != new:
        os.environ[key] = new
        return True
    return False


def _dependency_clamps() -> list[str]:
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


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    changed_keys: list[str] = []
    for key, (lo, hi, default) in _FLOAT_RANGES.items():
        if _clamp_float(key, lo, hi, default):
            changed_keys.append(key)
    for key, (lo, hi, default) in _INT_RANGES.items():
        if _clamp_int(key, lo, hi, default):
            changed_keys.append(key)
    for key in _BOOL_KEYS:
        if _normalize_bool(key):
            changed_keys.append(key)
    notes = _dependency_clamps()
    _INSTALLED = True
    logger.warning(
        "[BOARD RUNTIME SAFETY] installed changed=%s keys=%s notes=%s reconcile_rest_timeout=%s memory_fallback=%s",
        len(changed_keys),
        changed_keys[:40],
        notes,
        os.environ.get("EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC"),
        os.environ.get("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD RUNTIME SAFETY] auto install failed")


__all__ = ["install"]
