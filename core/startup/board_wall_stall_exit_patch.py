# ============================================================
# File   : core/startup/board_wall_stall_exit_patch.py
# Version: Ver04-EXIT-ACTIVE-BY-DEFAULT
# ------------------------------------------------------------
# 板情報を実運用に接続する runtime patch。
#
# ENTRY直前:
#   - final_entry_safety_guard の _board_guard を包み、
#     スプレッド/最良気配チェック後に複数段板の偏りを確認する。
#
# EXIT:
#   - EXIT pipeline はデフォルトで止めない。
#   - 明示的に AUTOSTOCK_TEMP_DISABLE_EXIT=1 または EXIT_TEMP_STOP=1 の時だけ停止する。
#   - 停止していない時は、板崩壊/壁食われ失速の早期EXITを既存どおり維持する。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "Ver04-EXIT-ACTIVE-BY-DEFAULT"
_INSTALLED = False
_ENTRY_INSTALLED = False
_EXIT_PIPELINE_STOP_INSTALLED = False
_ORIG_CHECK_NORMAL_EXIT = None
_ORIG_CHECK_TONOSAMA_EXIT = None
_ORIG_RUN_EXIT_PIPELINE = None
_ORIG_MAIN_RUN_EXIT_PIPELINE = None
_ORIG_BOARD_GUARD = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}:
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


def _exit_temp_disabled() -> bool:
    # 重要: デフォルトでは EXIT を止めない。明示指定の時だけ停止する。
    return _env_bool("AUTOSTOCK_TEMP_DISABLE_EXIT", False) or _env_bool("EXIT_TEMP_STOP", False)


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


def _patched_run_exit_pipeline(*args, **kwargs):
    logger.warning(
        "[EXIT TEMP STOP] run_exit_pipeline skipped AUTOSTOCK_TEMP_DISABLE_EXIT=%s EXIT_TEMP_STOP=%s",
        os.getenv("AUTOSTOCK_TEMP_DISABLE_EXIT"),
        os.getenv("EXIT_TEMP_STOP"),
    )
    return None


def _restore_exit_pipeline_if_needed() -> bool:
    """過去の temp stop wrapper が残っていれば、明示停止OFF時に元関数へ戻す。"""
    restored = False
    try:
        import trading.handlers.exit_handler as eh

        cur = getattr(eh, "run_exit_pipeline", None)
        original = getattr(cur, "_original", None)
        if callable(cur) and getattr(cur, "_exit_temp_stop_patch", False) and callable(original):
            eh.run_exit_pipeline = original
            restored = True

        main_mod = sys.modules.get("__main__")
        if main_mod is not None:
            main_cur = getattr(main_mod, "run_exit_pipeline", None)
            main_original = getattr(main_cur, "_original", None)
            if callable(main_cur) and getattr(main_cur, "_exit_temp_stop_patch", False) and callable(main_original):
                setattr(main_mod, "run_exit_pipeline", main_original)
                restored = True
    except Exception:
        logger.debug("[EXIT TEMP STOP] restore skipped", exc_info=True)
    if restored:
        logger.warning("[EXIT TEMP STOP] restored previous wrapper -> EXIT pipeline active version=%s", VERSION)
    return restored


def _install_exit_pipeline_temp_stop() -> bool:
    global _EXIT_PIPELINE_STOP_INSTALLED, _ORIG_RUN_EXIT_PIPELINE, _ORIG_MAIN_RUN_EXIT_PIPELINE
    if not _exit_temp_disabled():
        _restore_exit_pipeline_if_needed()
        logger.warning("[EXIT TEMP STOP] disabled by default/env -> EXIT pipeline remains active version=%s", VERSION)
        return False
    try:
        import trading.handlers.exit_handler as eh

        cur = getattr(eh, "run_exit_pipeline", None)
        if callable(cur) and not getattr(cur, "_exit_temp_stop_patch", False):
            _ORIG_RUN_EXIT_PIPELINE = cur
            _patched_run_exit_pipeline._exit_temp_stop_patch = True  # type: ignore[attr-defined]
            _patched_run_exit_pipeline._original = cur  # type: ignore[attr-defined]
            eh.run_exit_pipeline = _patched_run_exit_pipeline

        main_mod = sys.modules.get("__main__")
        if main_mod is not None:
            main_cur = getattr(main_mod, "run_exit_pipeline", None)
            if callable(main_cur) and not getattr(main_cur, "_exit_temp_stop_patch", False):
                _ORIG_MAIN_RUN_EXIT_PIPELINE = main_cur
                _patched_run_exit_pipeline._original = main_cur  # type: ignore[attr-defined]
                setattr(main_mod, "run_exit_pipeline", _patched_run_exit_pipeline)

        _EXIT_PIPELINE_STOP_INSTALLED = True
        logger.warning("[EXIT TEMP STOP] installed by explicit env: EXIT pipeline is temporarily stopped version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[EXIT TEMP STOP] install failed")
        return False


def _board_fast_exit_reason(pos: Any, price: float) -> str | None:
    """支え板崩壊EXIT → 壁食われ失速EXIT の順で確認する。"""
    if _exit_temp_disabled():
        return None

    symbol = _position_symbol(pos)
    side = _position_side(pos)
    if not symbol or price <= 0:
        return None

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
    if _exit_temp_disabled():
        return None
    reason = _board_fast_exit_reason(pos, price)
    if reason:
        return reason
    if callable(_ORIG_CHECK_NORMAL_EXIT):
        return _ORIG_CHECK_NORMAL_EXIT(pos, price, now)
    return None


def _patched_check_tonosama_exit(pos: Any, price: float, now):
    if _exit_temp_disabled():
        return None
    reason = _board_fast_exit_reason(pos, price)
    if reason:
        return reason
    if callable(_ORIG_CHECK_TONOSAMA_EXIT):
        return _ORIG_CHECK_TONOSAMA_EXIT(pos, price, now)
    return None


def _patched_board_guard(row: dict, symbol: str, side: str) -> bool:
    if callable(_ORIG_BOARD_GUARD):
        ok = bool(_ORIG_BOARD_GUARD(row, symbol, side))
        if not ok:
            return False

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
    ok_stop = _install_exit_pipeline_temp_stop()

    if _exit_temp_disabled():
        return bool(ok_entry or ok_stop)

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
            "[BOARD FAST EXIT PATCH] installed collapse=%s collapse_lookback=%s drop_ratio=%s wall_stall=%s wall_lookback=%s eaten_ratio=%s min_qty=%s max_move_pct=%s near_levels=%s exchange=%s entry_guard=%s version=%s",
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
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[BOARD FAST EXIT PATCH] install failed")
        return bool(ok_entry or ok_stop)
