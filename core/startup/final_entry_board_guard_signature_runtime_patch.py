# ============================================================
# File   : core/startup/final_entry_board_guard_signature_runtime_patch.py
# Version: V1-FINAL-BOARD-GUARD-SIGNATURE-COMPAT
# ------------------------------------------------------------
# 目的:
#   final_entry_safety_guard_patch._board_guard が別runtime patchにより
#   3引数版へ差し替わった後でも、4引数呼び出しで TypeError にならないようにする。
#
#   事象:
#     BOARD_GUARD_ERROR ... _patched_board_guard() takes 3 positional arguments but 4 were given
#     -> board_missing 扱いになり pending_keep のまま発注されない。
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

VERSION = "V1-FINAL-BOARD-GUARD-SIGNATURE-COMPAT"
_INSTALLED = False
_WATCHER_STARTED = False


def _call_flexible(fn: Callable[..., Any], row: dict, item: dict, symbol: str, side: str) -> bool:
    """Call board guard with the signature it actually supports."""
    try:
        return bool(fn(row, item, symbol, side))
    except TypeError as e4:
        msg = str(e4)
        # 3引数版: (row, symbol, side)
        try:
            logger.warning(
                "[FINAL BOARD GUARD SIGNATURE] fallback 4args->3args symbol=%s side=%s err=%s version=%s",
                symbol,
                side,
                msg,
                VERSION,
            )
            return bool(fn(row, symbol, side))
        except TypeError as e3:
            # 2引数版/keyword版の保険
            try:
                logger.warning(
                    "[FINAL BOARD GUARD SIGNATURE] fallback 3args->kwargs symbol=%s side=%s err3=%s version=%s",
                    symbol,
                    side,
                    e3,
                    VERSION,
                )
                return bool(fn(row=row, item=item, symbol=symbol, side=side))
            except Exception:
                raise e4
    except Exception:
        raise


def _wrap_board_guard(target: Any) -> bool:
    cur = getattr(target, "_board_guard", None)
    if not callable(cur):
        return False
    if getattr(cur, "_final_board_guard_signature_compat_v1", False):
        return True

    original = cur

    def _compat_board_guard(row: dict, item: dict | None = None, symbol: str | None = None, side: str | None = None, *args, **kwargs) -> bool:
        item_d = item if isinstance(item, dict) else {}
        sym = str(symbol or "")
        sd = str(side or "").upper()
        return _call_flexible(original, row, item_d, sym, sd)

    _compat_board_guard._final_board_guard_signature_compat_v1 = True  # type: ignore[attr-defined]
    _compat_board_guard._original = original  # type: ignore[attr-defined]
    target._board_guard = _compat_board_guard
    logger.warning(
        "[FINAL BOARD GUARD SIGNATURE] wrapped _board_guard original=%s version=%s",
        getattr(original, "__name__", type(original).__name__),
        VERSION,
    )
    return True


def _wrap_call_board_guard(target: Any) -> bool:
    cur = getattr(target, "_call_board_guard", None)
    if not callable(cur):
        return False
    if getattr(cur, "_final_board_guard_signature_call_v1", False):
        return True

    def _compat_call_board_guard(row: dict, item: dict, symbol: str, side: str) -> bool:
        try:
            bg = getattr(target, "_board_guard", None)
            if callable(bg):
                return _call_flexible(bg, row, item if isinstance(item, dict) else {}, str(symbol or ""), str(side or "").upper())
            return bool(cur(row, item, symbol, side))
        except Exception as e:
            logger.warning(
                "[FINAL BOARD GUARD SIGNATURE] BOARD_GUARD_ERROR_COMPAT symbol=%s side=%s error=%s version=%s",
                symbol,
                side,
                e,
                VERSION,
            )
            try:
                fallback = getattr(target, "_board_missing_fallback_ok", None)
                if callable(fallback):
                    return bool(fallback(row, item if isinstance(item, dict) else {}, symbol, side))
            except Exception:
                pass
            return False

    _compat_call_board_guard._final_board_guard_signature_call_v1 = True  # type: ignore[attr-defined]
    _compat_call_board_guard._original = cur  # type: ignore[attr-defined]
    target._call_board_guard = _compat_call_board_guard
    logger.warning("[FINAL BOARD GUARD SIGNATURE] wrapped _call_board_guard version=%s", VERSION)
    return True


def _patch_once() -> bool:
    try:
        import core.startup.final_entry_safety_guard_patch as target
        ok1 = _wrap_board_guard(target)
        ok2 = _wrap_call_board_guard(target)
        return bool(ok1 or ok2)
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIGNATURE] patch_once failed")
        return False


def _watch() -> None:
    for i in range(60):
        ok = _patch_once()
        if i in (0, 10, 30, 59):
            logger.warning("[FINAL BOARD GUARD SIGNATURE] enforce i=%s/60 ok=%s version=%s", i, ok, VERSION)
        time.sleep(1.0)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    ok = _patch_once()
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch, daemon=True, name="final-board-guard-signature-compat").start()
    logger.warning("[FINAL BOARD GUARD SIGNATURE] installed ok=%s watcher=%s version=%s", ok, _WATCHER_STARTED, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[FINAL BOARD GUARD SIGNATURE] auto install failed")


__all__ = ["install", "VERSION"]
