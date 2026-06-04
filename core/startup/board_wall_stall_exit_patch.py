# ============================================================
# File   : core/startup/board_wall_stall_exit_patch.py
# Version: Ver02-BOARD-ENTRY-IMBALANCE-AND-FAST-EXIT
# ------------------------------------------------------------
# 板情報を実運用に接続する runtime patch。
#
# ENTRY直前:
#   - final_entry_safety_guard の _board_guard を包み、
#     スプレッド/最良気配チェック後に複数段板の偏りを確認する。
#   - BUYなのに買い板が弱い、SELLなのに買い板が強い、
#     進行方向の反対側に巨大壁がある場合だけ拒否する。
#
# EXIT:
#   - BUY保有中に買い支え板が崩れたら早期EXIT。
#   - SELL保有中に売り支え板が崩れたら早期EXIT。
#   - 既存の「壁が食われているのに価格が止まる」EXITも維持。
#
# 環境変数:
#   ENTRY_BOARD_IMBALANCE_ENABLED=1
#   ENTRY_BOARD_IMBALANCE_LEVELS=5
#   ENTRY_BOARD_BUY_MIN_BID_ASK_RATIO=0.70
#   ENTRY_BOARD_SELL_MAX_BID_ASK_RATIO=1.30
#   ENTRY_BOARD_WALL_REJECT_ENABLED=1
#   ENTRY_BOARD_WALL_REJECT_RATIO=3.00
#   ENTRY_BOARD_WALL_MIN_QTY=1500
#
#   EXIT_BOARD_COLLAPSE_ENABLED=1
#   EXIT_BOARD_COLLAPSE_LEVELS=5
#   EXIT_BOARD_COLLAPSE_LOOKBACK_SEC=6
#   EXIT_BOARD_COLLAPSE_SUPPORT_DROP_RATIO=0.45
#   EXIT_BOARD_COLLAPSE_MIN_SUPPORT_QTY=800
#   EXIT_BOARD_BUY_MIN_BID_ASK_RATIO=0.55
#   EXIT_BOARD_SELL_MAX_BID_ASK_RATIO=1.80
#
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
_ENTRY_INSTALLED = False
_ORIG_CHECK_NORMAL_EXIT = None
_ORIG_CHECK_TONOSAMA_EXIT = None
_ORIG_BOARD_GUARD = None


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


def _board_fast_exit_reason(pos: Any, price: float) -> str | None:
    """支え板崩壊EXIT → 壁食われ失速EXIT の順で確認する。"""
    symbol = _position_symbol(pos)
    side = _position_side(pos)
    if not symbol or price <= 0:
        return None

    # 1) 保有方向の支え板崩壊を先に見る。損切/撤退を速くする目的。
    try:
        from trading.board.board_signal import analyze_exit_board_collapse

        detail = analyze_exit_board_collapse(
            symbol,
            position_side=side,
            current_price=float(price),
            exchange=_env_int("EXIT_BOARD_WALL_EXCHANGE", 1),
        )
        if detail:
            reason = str(detail.get("reason") or "BOARD_SUPPORT_COLLAPSE_EXIT")
            logger.warning(
                "[BOARD FAST EXIT PATCH] EXIT symbol=%s side=%s price=%s reason=%s detail=%s",
                symbol,
                side,
                price,
                reason,
                detail,
            )
            return reason
    except Exception:
        logger.debug("[BOARD FAST EXIT PATCH] collapse analyze failed symbol=%s", symbol, exc_info=True)

    # 2) 既存ロジック: 壁が食われているのに価格が伸びない場合。
    if not _env_bool("EXIT_BOARD_WALL_STALL_ENABLED", True):
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
    reason = _board_fast_exit_reason(pos, price)
    if reason:
        return reason
    if callable(_ORIG_CHECK_NORMAL_EXIT):
        return _ORIG_CHECK_NORMAL_EXIT(pos, price, now)
    return None


def _patched_check_tonosama_exit(pos: Any, price: float, now):
    reason = _board_fast_exit_reason(pos, price)
    if reason:
        return reason
    if callable(_ORIG_CHECK_TONOSAMA_EXIT):
        return _ORIG_CHECK_TONOSAMA_EXIT(pos, price, now)
    return None


def _patched_board_guard(row: dict, symbol: str, side: str) -> bool:
    # 既存の最良気配/スプレッド/薄板チェックを先に通す。
    if callable(_ORIG_BOARD_GUARD):
        ok = bool(_ORIG_BOARD_GUARD(row, symbol, side))
        if not ok:
            return False

    # 複数段板の偏りチェック。取得失敗時は fail-open。
    try:
        from trading.board.board_signal import analyze_entry_board_imbalance
        import core.startup.final_entry_safety_guard_patch as fsg

        detail = analyze_entry_board_imbalance(
            symbol,
            side=side,
            exchange=_env_int("ENTRY_BOARD_EXCHANGE", _env_int("EXIT_BOARD_WALL_EXCHANGE", 1)),
        )
        if detail:
            reason = str(detail.get("reason") or "entry_board_imbalance")
            try:
                fsg._log_ng(reason, symbol, side, **detail)
            except Exception:
                logger.warning("[BOARD ENTRY IMBALANCE PATCH] NG symbol=%s side=%s detail=%s", symbol, side, detail)
            return False
    except Exception:
        logger.debug("[BOARD ENTRY IMBALANCE PATCH] analyze failed symbol=%s side=%s", symbol, side, exc_info=True)

    return True


def _install_entry_board_imbalance_guard() -> bool:
    global _ENTRY_INSTALLED, _ORIG_BOARD_GUARD
    if _ENTRY_INSTALLED:
        return True
    try:
        import core.startup.final_entry_safety_guard_patch as fsg

        cur = getattr(fsg, "_board_guard", None)
        if not callable(cur):
            logger.warning("[BOARD ENTRY IMBALANCE PATCH] final_entry_safety_guard._board_guard unavailable")
            return False
        if getattr(cur, "_board_entry_imbalance_patch", False):
            _ENTRY_INSTALLED = True
            return True

        _ORIG_BOARD_GUARD = cur
        _patched_board_guard._board_entry_imbalance_patch = True  # type: ignore[attr-defined]
        _patched_board_guard._original = cur  # type: ignore[attr-defined]
        fsg._board_guard = _patched_board_guard
        _ENTRY_INSTALLED = True
        logger.warning(
            "[BOARD ENTRY IMBALANCE PATCH] installed enabled=%s levels=%s buy_min_ratio=%s sell_max_ratio=%s wall_reject=%s wall_ratio=%s wall_min_qty=%s",
            _env_bool("ENTRY_BOARD_IMBALANCE_ENABLED", True),
            os.getenv("ENTRY_BOARD_IMBALANCE_LEVELS", "5"),
            os.getenv("ENTRY_BOARD_BUY_MIN_BID_ASK_RATIO", "0.70"),
            os.getenv("ENTRY_BOARD_SELL_MAX_BID_ASK_RATIO", "1.30"),
            _env_bool("ENTRY_BOARD_WALL_REJECT_ENABLED", True),
            os.getenv("ENTRY_BOARD_WALL_REJECT_RATIO", "3.00"),
            os.getenv("ENTRY_BOARD_WALL_MIN_QTY", "1500"),
        )
        return True
    except Exception:
        logger.exception("[BOARD ENTRY IMBALANCE PATCH] install failed")
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_CHECK_NORMAL_EXIT, _ORIG_CHECK_TONOSAMA_EXIT
    ok_entry = _install_entry_board_imbalance_guard()

    try:
        import trading.handlers.exit_handler as eh

        if _INSTALLED:
            return bool(ok_entry or True)

        cur_normal = getattr(eh, "check_normal_exit", None)
        cur_tono = getattr(eh, "check_tonosama_exit", None)

        if getattr(cur_normal, "_board_wall_stall_exit_patch", False):
            _INSTALLED = True
            return bool(ok_entry or True)
        if not callable(cur_normal):
            logger.error("[BOARD FAST EXIT PATCH] check_normal_exit unavailable")
            return bool(ok_entry)

        _ORIG_CHECK_NORMAL_EXIT = cur_normal
        _ORIG_CHECK_TONOSAMA_EXIT = cur_tono if callable(cur_tono) else None

        _patched_check_normal_exit._board_wall_stall_exit_patch = True  # type: ignore[attr-defined]
        _patched_check_tonosama_exit._board_wall_stall_exit_patch = True  # type: ignore[attr-defined]

        eh.check_normal_exit = _patched_check_normal_exit
        if callable(cur_tono):
            eh.check_tonosama_exit = _patched_check_tonosama_exit

        _INSTALLED = True
        logger.warning(
            "[BOARD FAST EXIT PATCH] installed collapse=%s collapse_lookback=%s drop_ratio=%s wall_stall=%s wall_lookback=%s eaten_ratio=%s min_qty=%s max_move_pct=%s near_levels=%s exchange=%s entry_guard=%s",
            _env_bool("EXIT_BOARD_COLLAPSE_ENABLED", True),
            os.getenv("EXIT_BOARD_COLLAPSE_LOOKBACK_SEC", "6"),
            os.getenv("EXIT_BOARD_COLLAPSE_SUPPORT_DROP_RATIO", "0.45"),
            _env_bool("EXIT_BOARD_WALL_STALL_ENABLED", True),
            os.getenv("EXIT_BOARD_WALL_LOOKBACK_SEC", "8"),
            os.getenv("EXIT_BOARD_WALL_MIN_EATEN_RATIO", "0.35"),
            os.getenv("EXIT_BOARD_WALL_MIN_QTY", "1000"),
            os.getenv("EXIT_BOARD_WALL_STALL_MAX_MOVE_PCT", "0.0008"),
            os.getenv("EXIT_BOARD_WALL_NEAR_LEVELS", "3"),
            os.getenv("EXIT_BOARD_WALL_EXCHANGE", "1"),
            ok_entry,
        )
        return True
    except Exception:
        logger.exception("[BOARD FAST EXIT PATCH] install failed")
        return bool(ok_entry)


try:
    install()
except Exception:
    logger.exception("[BOARD FAST EXIT PATCH] auto install failed")


__all__ = ["install"]
