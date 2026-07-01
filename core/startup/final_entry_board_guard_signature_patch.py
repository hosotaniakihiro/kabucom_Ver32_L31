# ============================================================
# File   : core/startup/final_entry_board_guard_signature_patch.py
# Version: V1-FINAL-ENTRY-BOARD-GUARD-SIGNATURE-COMPAT
# ------------------------------------------------------------
# final_entry_safety_guard_patch._board_guard が後続runtime patchで
# 3引数版へ差し替わった場合でも、_call_board_guard 側で複数の
# 呼び出しシグネチャを順番に試して TypeError 停止を防ぐ。
#
# Fixes:
#   _patched_board_guard() takes 3 positional arguments but 4 were given
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-FINAL-ENTRY-BOARD-GUARD-SIGNATURE-COMPAT"
_INSTALLED = False
_WATCHER_STARTED = False


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s in {"BUY", "LONG", "2", "買", "買い"}:
            return "BUY"
        if s in {"SELL", "SHORT", "1", "売", "売り"}:
            return "SELL"
        return s
    except Exception:
        return ""


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        try:
            v = row.get(key)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _board_missing_fallback(target: Any, row: dict[str, Any], item: dict[str, Any], symbol: str, side: str) -> bool:
    try:
        fn = getattr(target, "_board_missing_fallback_ok", None)
        if callable(fn):
            return bool(fn(row, item, symbol, side))
    except Exception:
        logger.debug("[FINAL ENTRY BOARD SIGNATURE PATCH] board_missing fallback failed", exc_info=True)
    return False


def _mark_board_missing(target: Any, item: dict[str, Any], symbol: str, side: str) -> None:
    try:
        mark = getattr(target, "_mark_skip", None)
        if callable(mark):
            mark(item, "board_missing", bid=0.0, ask=0.0, retryable=True)
    except Exception:
        pass
    try:
        log_ng = getattr(target, "_log_ng", None)
        if callable(log_ng):
            log_ng("board_missing", symbol, side, bid=0.0, ask=0.0, retryable=True, pending_action="keep", compat="signature_patch")
    except Exception:
        pass


def _call_board_guard_flexible(target: Any, row: Any, item: dict[str, Any], symbol: str, side: str) -> bool:
    row_d = _row_to_dict(row)
    item_d = item if isinstance(item, dict) else {}
    sym = _norm_symbol(symbol or _first(row_d, ("symbol", "Symbol", "code", "銘柄コード"), ""))
    sd = _norm_side(side or _first(row_d, ("side", "entry_decision", "ai_side"), ""))

    guard = getattr(target, "_board_guard", None)
    if not callable(guard):
        logger.warning("[FINAL ENTRY BOARD SIGNATURE PATCH] _board_guard missing symbol=%s side=%s", sym, sd)
        _mark_board_missing(target, item_d, sym, sd)
        return _board_missing_fallback(target, row_d, item_d, sym, sd)

    attempts = (
        (row_d, item_d, sym, sd),
        (row_d, sym, sd),
        (row_d, item_d, sym),
        (row_d, item_d),
        (row_d,),
    )
    last_err: Exception | None = None
    for args in attempts:
        try:
            return bool(guard(*args))
        except TypeError as e:
            last_err = e
            continue
        except Exception as e:
            logger.warning(
                "[FINAL ENTRY BOARD SIGNATURE PATCH] board guard raised symbol=%s side=%s guard=%s args=%s error=%s",
                sym,
                sd,
                getattr(guard, "__name__", repr(guard)),
                len(args),
                e,
            )
            _mark_board_missing(target, item_d, sym, sd)
            return _board_missing_fallback(target, row_d, item_d, sym, sd)

    logger.warning(
        "[FINAL ENTRY BOARD SIGNATURE PATCH] incompatible board guard signature symbol=%s side=%s guard=%s last_error=%s",
        sym,
        sd,
        getattr(guard, "__name__", repr(guard)),
        last_err,
    )
    _mark_board_missing(target, item_d, sym, sd)
    return _board_missing_fallback(target, row_d, item_d, sym, sd)


def _patch_once(reason: str = "install") -> bool:
    try:
        import core.startup.final_entry_safety_guard_patch as target

        cur = getattr(target, "_call_board_guard", None)
        if callable(cur) and getattr(cur, "_final_entry_board_signature_patch_v1", False):
            return True

        def _patched_call_board_guard(row: dict, item: dict, symbol: str, side: str) -> bool:
            return _call_board_guard_flexible(target, row, item, symbol, side)

        _patched_call_board_guard._final_entry_board_signature_patch_v1 = True  # type: ignore[attr-defined]
        _patched_call_board_guard._original = cur  # type: ignore[attr-defined]
        target._call_board_guard = _patched_call_board_guard
        logger.warning("[FINAL ENTRY BOARD SIGNATURE PATCH] installed reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[FINAL ENTRY BOARD SIGNATURE PATCH] install failed reason=%s", reason)
        return False


def _watcher() -> None:
    for i in range(120):
        try:
            _patch_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[FINAL ENTRY BOARD SIGNATURE PATCH] watcher failed", exc_info=True)
        time.sleep(0.5)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    ok = _patch_once("install")
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="final-entry-board-signature-watch", daemon=True).start()
        logger.warning("[FINAL ENTRY BOARD SIGNATURE PATCH] watcher started")
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[FINAL ENTRY BOARD SIGNATURE PATCH] auto install failed")


__all__ = ["install"]
