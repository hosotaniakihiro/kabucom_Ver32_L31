# ============================================================
# File   : core/startup/board_wall_stall_exit_patch.py
# Version: Ver01-BOARD-WALL-EATEN-STALL-EXIT
# ------------------------------------------------------------
# EXIT直前判定に「板が食われているのに株価が止まる」検知を追加する。
#
# BUY保有中:
#   上の厚い売り板が減っているのに株価が上抜けない場合、
#   買い圧力が一巡して反転する可能性があるため EXIT。
#
# SELL保有中:
#   下の厚い買い板が減っているのに株価が下抜けない場合、
#   売り圧力が一巡して反転する可能性があるため EXIT。
#
# 環境変数:
#   EXIT_BOARD_WALL_STALL_ENABLED=1
#   EXIT_BOARD_WALL_LOOKBACK_SEC=8
#   EXIT_BOARD_WALL_MIN_EATEN_RATIO=0.35
#   EXIT_BOARD_WALL_MIN_QTY=1000
#   EXIT_BOARD_WALL_STALL_MAX_MOVE_PCT=0.0008
#   EXIT_BOARD_WALL_NEAR_LEVELS=3
#   EXIT_BOARD_WALL_EXCHANGE=1
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_CHECK_NORMAL_EXIT = None
_ORIG_CHECK_TONOSAMA_EXIT = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _position_symbol(pos: Any) -> str:
    return str(
        _get(pos, "symbol")
        or _get(pos, "Symbol")
        or _get(pos, "stock_code")
        or ""
    ).strip()


def _position_side(pos: Any) -> str:
    return str(_get(pos, "side") or _get(pos, "Side") or "BUY").upper()


def _board_wall_exit_reason(pos: Any, price: float) -> str | None:
    if not _env_bool("EXIT_BOARD_WALL_STALL_ENABLED", True):
        return None

    symbol = _position_symbol(pos)
    side = _position_side(pos)
    if not symbol or price <= 0:
        return None

    try:
        from trading.board.board_state import analyze_wall_eaten_stall
        detail = analyze_wall_eaten_stall(
            symbol,
            position_side=side,
            current_price=float(price),
            exchange=_env_int("EXIT_BOARD_WALL_EXCHANGE", 1),
        )
        if not detail:
            return None

        reason = str(detail.get("reason") or "BOARD_WALL_EATEN_STALL_EXIT")
        logger.warning(
            "[BOARD WALL STALL EXIT PATCH] EXIT symbol=%s side=%s price=%s reason=%s detail=%s",
            symbol,
            side,
            price,
            reason,
            detail,
        )
        return reason
    except Exception:
        logger.debug("[BOARD WALL STALL EXIT PATCH] analyze failed symbol=%s", symbol, exc_info=True)
        return None


def _patched_check_normal_exit(pos: Any, price: float, now):
    reason = _board_wall_exit_reason(pos, price)
    if reason:
        return reason
    if callable(_ORIG_CHECK_NORMAL_EXIT):
        return _ORIG_CHECK_NORMAL_EXIT(pos, price, now)
    return None


def _patched_check_tonosama_exit(pos: Any, price: float, now):
    reason = _board_wall_exit_reason(pos, price)
    if reason:
        return reason
    if callable(_ORIG_CHECK_TONOSAMA_EXIT):
        return _ORIG_CHECK_TONOSAMA_EXIT(pos, price, now)
    return None


def install() -> bool:
    global _INSTALLED, _ORIG_CHECK_NORMAL_EXIT, _ORIG_CHECK_TONOSAMA_EXIT
    try:
        import trading.handlers.exit_handler as eh

        if _INSTALLED:
            return True

        cur_normal = getattr(eh, "check_normal_exit", None)
        cur_tono = getattr(eh, "check_tonosama_exit", None)

        if getattr(cur_normal, "_board_wall_stall_exit_patch", False):
            _INSTALLED = True
            return True

        if not callable(cur_normal):
            logger.error("[BOARD WALL STALL EXIT PATCH] check_normal_exit unavailable")
            return False

        _ORIG_CHECK_NORMAL_EXIT = cur_normal
        _ORIG_CHECK_TONOSAMA_EXIT = cur_tono if callable(cur_tono) else None

        _patched_check_normal_exit._board_wall_stall_exit_patch = True  # type: ignore[attr-defined]
        _patched_check_tonosama_exit._board_wall_stall_exit_patch = True  # type: ignore[attr-defined]

        eh.check_normal_exit = _patched_check_normal_exit
        if callable(cur_tono):
            eh.check_tonosama_exit = _patched_check_tonosama_exit

        _INSTALLED = True
        logger.warning(
            "[BOARD WALL STALL EXIT PATCH] installed enabled=%s lookback_sec=%s eaten_ratio=%s min_qty=%s max_move_pct=%s near_levels=%s exchange=%s",
            _env_bool("EXIT_BOARD_WALL_STALL_ENABLED", True),
            os.getenv("EXIT_BOARD_WALL_LOOKBACK_SEC", "8"),
            os.getenv("EXIT_BOARD_WALL_MIN_EATEN_RATIO", "0.35"),
            os.getenv("EXIT_BOARD_WALL_MIN_QTY", "1000"),
            os.getenv("EXIT_BOARD_WALL_STALL_MAX_MOVE_PCT", "0.0008"),
            os.getenv("EXIT_BOARD_WALL_NEAR_LEVELS", "3"),
            os.getenv("EXIT_BOARD_WALL_EXCHANGE", "1"),
        )
        return True
    except Exception:
        logger.exception("[BOARD WALL STALL EXIT PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[BOARD WALL STALL EXIT PATCH] auto install failed")


__all__ = ["install"]
