# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/final_board_guard_signature_compat_patch.py
# Version: V2-MARK-NATIVE-GUARD-NO-REWRAP
# ------------------------------------------------------------
# Purpose:
#   final_entry_safety_guard_patch Ver12 以降は _board_guard 自体が
#   4引数対応済みのため、ここで再wrapしない。
#
# V2:
#   - 古い V1 watcher が _board_guard を1秒ごとに再wrapする問題を停止。
#   - summary_ai_entry_hook_dataframe_truth_patch 側の旧compat watcherが
#     再wrapしないよう、現在の native guard に signature marker を付与。
#   - 既に旧compat wrapper が挟まっている場合は _original / compat_original を剥がして戻す。
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V2-MARK-NATIVE-GUARD-NO-REWRAP"
_WATCHER_STARTED = False
_INSTALLED = False
_LAST_TARGET_ID: int | None = None


def _is_legacy_wrapper(fn: Any) -> bool:
    return bool(
        getattr(fn, "_final_board_guard_signature_compat_v1", False)
        or getattr(fn, "_final_entry_board_guard_compat", False)
        or getattr(fn, "_final_entry_board_guard_compat_v15", False)
        or getattr(fn, "_final_entry_board_guard_compat_v16", False)
    )


def _unwrap(fn: Any) -> Any:
    seen: set[int] = set()
    cur = fn
    while callable(cur) and id(cur) not in seen:
        seen.add(id(cur))
        nxt = (
            getattr(cur, "_final_entry_board_guard_compat_original", None)
            or getattr(cur, "_original", None)
            or getattr(cur, "_original_board_guard", None)
        )
        if callable(nxt) and nxt is not cur:
            cur = nxt
            continue
        break
    return cur


def _mark_signature_safe(fn: Any) -> Any:
    try:
        setattr(fn, "_final_board_guard_signature_v2", True)
        setattr(fn, "_final_board_guard_signature_runtime", True)
        setattr(fn, "_final_board_guard_signature_compat_v2", True)
    except Exception:
        pass
    return fn


def _apply(reason: str = "install") -> bool:
    global _LAST_TARGET_ID
    try:
        import core.startup.final_entry_safety_guard_patch as fsg

        cur = getattr(fsg, "_board_guard", None)
        if not callable(cur):
            logger.warning("[FINAL BOARD GUARD SIG COMPAT] target _board_guard missing reason=%s version=%s", reason, VERSION)
            return False

        base = _unwrap(cur)
        if not callable(base):
            base = cur

        _mark_signature_safe(base)
        try:
            fsg._board_guard = base
            fsg._patched_board_guard = base
        except Exception:
            pass

        cur_id = id(base)
        if _LAST_TARGET_ID != cur_id or _is_legacy_wrapper(cur):
            logger.warning(
                "[FINAL BOARD GUARD SIG COMPAT] native guard marked reason=%s unwrapped=%s cur=%s base=%s version=%s",
                reason,
                _is_legacy_wrapper(cur),
                getattr(cur, "__name__", type(cur).__name__),
                getattr(base, "__name__", type(base).__name__),
                VERSION,
            )
        _LAST_TARGET_ID = cur_id
        return True
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIG COMPAT] apply failed reason=%s version=%s", reason, VERSION)
        return False


def _watcher() -> None:
    try:
        stable = 0
        last_id = None
        for i in range(20):
            time.sleep(1.0)
            ok = _apply(reason=f"watcher:{i + 1}")
            cur_id = _LAST_TARGET_ID
            stable = stable + 1 if ok and cur_id == last_id else 0
            last_id = cur_id
            if stable >= 3:
                logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher stable exit i=%s version=%s", i + 1, VERSION)
                return
        logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher done version=%s", VERSION)
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIG COMPAT] watcher failed version=%s", VERSION)


def _start_watcher() -> None:
    global _WATCHER_STARTED
    if _WATCHER_STARTED:
        return
    _WATCHER_STARTED = True
    threading.Thread(target=_watcher, name="final-board-guard-signature-compat", daemon=True).start()
    logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher started version=%s", VERSION)


def install() -> bool:
    global _INSTALLED
    ok = _apply("install")
    _start_watcher()
    _INSTALLED = bool(ok)
    logger.warning("[FINAL BOARD GUARD SIG COMPAT] installed=%s version=%s", _INSTALLED, VERSION)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[FINAL BOARD GUARD SIG COMPAT] auto install failed")


__all__ = ["VERSION", "install"]
