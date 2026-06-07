# ============================================================
# File   : core/startup/board_runtime_diagnostics_patch.py
# Version: V1.1-BOARD-RUNTIME-DIAGNOSTICS-RECONCILE-REST
# ------------------------------------------------------------
# 起動時にRESTフル板/未約定/返済CLOSING関連の実効設定を
# 1回だけまとめてログ出力する。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False

_GROUPS = {
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
}


def _v(key: str) -> str:
    val = os.environ.get(key)
    if val is None or str(val).strip() == "":
        return "<unset>"
    return str(val).strip()


def _bool_on(key: str) -> bool:
    return _v(key).lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _summary_line(name: str, keys: list[str]) -> str:
    return f"[BOARD RUNTIME DIAG] {name} " + " ".join(f"{k}={_v(k)}" for k in keys)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    for name, keys in _GROUPS.items():
        logger.warning(_summary_line(name, keys))

    logger.warning(
        "[BOARD RUNTIME DIAG] EFFECTIVE entry_rest=%s entry_imbalance=%s entry_double_check=%s exit_rest=%s exit_pending_close=%s exit_reprice=%s exit_fill_confirm=%s exit_stale_reconcile=%s reconcile_memory_fallback=%s",
        _bool_on("ENTRY_REST_FULL_BOARD_ENABLED"),
        _bool_on("ENTRY_REST_FULL_BOARD_IMBALANCE_GUARD_ENABLED"),
        _bool_on("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_ENABLED"),
        _bool_on("EXIT_REST_FULL_BOARD_ENABLED"),
        _bool_on("EXIT_LIMIT_PENDING_CLOSE_ENABLED"),
        _bool_on("EXIT_UNFILLED_REPRICE_ENABLED"),
        _bool_on("EXIT_FILL_CONFIRM_ENABLED"),
        _bool_on("EXIT_CLOSING_RECONCILE_ENABLED"),
        _bool_on("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK"),
    )
    _INSTALLED = True
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD RUNTIME DIAG] auto install failed")


__all__ = ["install"]
