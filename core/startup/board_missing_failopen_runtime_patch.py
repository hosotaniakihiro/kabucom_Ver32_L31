# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/board_missing_failopen_runtime_patch.py
# Version: V1.3-FORCE-ALLOW-WITHOUT-BOARD
# ------------------------------------------------------------
# Purpose:
#   - PUSH A/B ローテーション境界などで板が一時的に取れない場合、
#     final_entry_safety_guard の流動性/score条件を満たす候補は
#     小ロットで fail-open する。
#   - main.py 側ではPUSH DB保存なしの方針を維持する。
#   - final_entry_safety_guard_patch が先に ENTRY_ALLOW_ENTRY_WITHOUT_BOARD=0 を
#     setdefault 済みでも、main runtime では保護fail-openを強制有効化する。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1.3-FORCE-ALLOW-WITHOUT-BOARD"
_INSTALLED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _patch_final_guard() -> bool:
    try:
        import core.startup.final_entry_safety_guard_patch as fsg
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] import final_entry_safety_guard_patch failed")
        return False

    if getattr(fsg, "_BOARD_MISSING_FAILOPEN_PATCHED_V13", False):
        return True

    old_board_guard = getattr(fsg, "_board_guard", None)
    old_patched_board_guard = getattr(fsg, "_patched_board_guard", None)

    def _board_guard_failopen(row: Any, item: Any = None, symbol: Any = None, side: Any = None, *args: Any, **kwargs: Any) -> bool:
        # 3引数互換: guard(row, symbol, side)
        if side is None and symbol is not None and not isinstance(item, dict):
            side = symbol
            symbol = item
            item = None

        row_d = fsg._row_to_dict(row)
        item_d = item if isinstance(item, dict) else {}
        symbol_s = fsg._norm_symbol(symbol or fsg._first(row_d, ("symbol", "Symbol", "code", "銘柄コード"), ""))
        side_s = fsg._norm_side(side or fsg._first(row_d, ("side", "entry_decision", "ai_side"), ""))

        if not fsg._env_bool("ENTRY_BOARD_GUARD_ENABLED", True):
            return True

        bid, ask, bid_qty, ask_qty = fsg._extract_bid_ask_from_row(row_d)
        if bid <= 0 or ask <= 0:
            try:
                bid2, ask2, bidq2, askq2 = fsg._try_get_bid_ask_from_api(symbol_s, side_s, "final_entry_safety_guard")
            except TypeError:
                try:
                    bid2, ask2, bidq2, askq2 = fsg._try_get_bid_ask_from_api(symbol_s)
                except Exception:
                    bid2 = ask2 = bidq2 = askq2 = 0.0
            except Exception:
                bid2 = ask2 = bidq2 = askq2 = 0.0
            bid = bid or bid2
            ask = ask or ask2
            bid_qty = bid_qty or bidq2
            ask_qty = ask_qty or askq2

        if bid <= 0 or ask <= 0:
            if _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", False):
                fsg._log_ng("board_missing", symbol_s, side_s, bid=bid, ask=ask, message="板が取れないため新規エントリー停止")
                logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_HARD_BLOCK symbol=%s side=%s bid=%s ask=%s", symbol_s, side_s, bid, ask)
                return False

            if fsg._env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", True):
                ok = False
                try:
                    ok = bool(fsg._board_missing_fallback_ok(row_d, item_d, symbol_s, side_s))
                except Exception:
                    logger.debug("[BOARD MISSING FAILOPEN] protected fallback check failed symbol=%s side=%s", symbol_s, side_s, exc_info=True)
                    ok = False

                if ok:
                    logger.warning(
                        "[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_PROTECTED_FAILOPEN symbol=%s side=%s bid=%s ask=%s version=%s",
                        symbol_s,
                        side_s,
                        bid,
                        ask,
                        VERSION,
                    )
                    return True

            fsg._log_ng("board_missing", symbol_s, side_s, bid=bid, ask=ask, message="板が取れず、保護条件も未達のため新規エントリー停止")
            return False

        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
        max_spread = fsg._env_float("ENTRY_MAX_SPREAD_PCT", 0.15)
        min_best_qty = fsg._env_float("ENTRY_MIN_BEST_BOARD_QTY", 100.0)
        if spread_pct > max_spread:
            fsg._log_ng("spread_too_wide", symbol_s, side_s, bid=bid, ask=ask, spread_pct=spread_pct, max_spread=max_spread)
            return False
        if side_s == "BUY" and ask_qty > 0 and ask_qty < min_best_qty:
            fsg._log_ng("ask_board_too_thin", symbol_s, side_s, ask_qty=ask_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
            return False
        if side_s == "SELL" and bid_qty > 0 and bid_qty < min_best_qty:
            fsg._log_ng("bid_board_too_thin", symbol_s, side_s, bid_qty=bid_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
            return False
        logger.info(
            "[FINAL ENTRY SAFETY GUARD] BOARD_OK symbol=%s side=%s bid=%.4f ask=%.4f spread_pct=%.4f bid_qty=%.0f ask_qty=%.0f",
            symbol_s,
            side_s,
            bid,
            ask,
            spread_pct,
            bid_qty,
            ask_qty,
        )
        return True

    _board_guard_failopen._board_missing_failopen_v1 = True  # type: ignore[attr-defined]
    _board_guard_failopen._board_missing_failopen_v13 = True  # type: ignore[attr-defined]
    _board_guard_failopen._original_board_guard = old_board_guard  # type: ignore[attr-defined]
    _board_guard_failopen._original_patched_board_guard = old_patched_board_guard  # type: ignore[attr-defined]
    fsg._board_guard = _board_guard_failopen
    fsg._patched_board_guard = _board_guard_failopen
    fsg._BOARD_MISSING_FAILOPEN_PATCHED_V1 = True
    fsg._BOARD_MISSING_FAILOPEN_PATCHED_V13 = True
    logger.warning("[BOARD MISSING FAILOPEN] final_entry_safety_guard board guard patched version=%s", VERSION)
    return True


def _install_summary_ai_lock_retry() -> bool:
    try:
        from core.startup import summary_ai_lock_retry_runtime_patch as lr
        fn = getattr(lr, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[BOARD MISSING FAILOPEN] summary_ai_lock_retry installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] summary_ai_lock_retry install failed")
        return False


def _install_summary_entry_stale_rescue() -> bool:
    try:
        from core.startup import summary_entry_stale_pending_rescue_patch as sr
        fn = getattr(sr, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[BOARD MISSING FAILOPEN] summary_entry_stale_rescue installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] summary_entry_stale_rescue install failed")
        return False


def install() -> bool:
    global _INSTALLED
    # final_entry_safety_guard_patch が先に ENTRY_ALLOW_ENTRY_WITHOUT_BOARD=0 を setdefault しているため、
    # ここは setdefault ではなく明示代入にする。強制ブロックしたい場合だけ *_FORCE で止める。
    os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK_FORCE", "0")
    os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD_FORCE", "1")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", "30000")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", "10000000")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", "200")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", "0.90")
    os.environ.setdefault("ENTRY_BOARD_MISSING_QTY_RATIO", "0.50")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_COUNT", "0")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_COUNT", "1")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_WAIT_SEC", "0.2")

    board_ok = _patch_final_guard()
    lock_retry_ok = _install_summary_ai_lock_retry()
    stale_rescue_ok = _install_summary_entry_stale_rescue()
    ok = bool(board_ok or lock_retry_ok or stale_rescue_ok)
    _INSTALLED = bool(ok)
    logger.warning(
        "[BOARD MISSING FAILOPEN] installed=%s board_ok=%s lock_retry_ok=%s stale_rescue_ok=%s hard_block=%s allow_without_board=%s qty_ratio=%s version=%s",
        ok,
        board_ok,
        lock_retry_ok,
        stale_rescue_ok,
        os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK"),
        os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
        os.getenv("ENTRY_BOARD_MISSING_QTY_RATIO"),
        VERSION,
    )
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[BOARD MISSING FAILOPEN] auto install failed")


__all__ = ["VERSION", "install"]
