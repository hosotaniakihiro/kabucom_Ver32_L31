# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/board_missing_failopen_runtime_patch.py
# Version: V1.8-DISABLED-SAFE-BOARD-REQUIRED-SIG-COMPAT
# ------------------------------------------------------------
# 以前の V1.6 は SUMMARY_AI の板なし発注を fail-open していた。
# 2026-07-01 の 429/API実行回数エラー・orig=wrapped 対策後は、
# final_entry_safety_guard v10 / board_retry v17 を優先し、このpatchは
# 板なし発注を再有効化しない安全no-opとして残す。
#
# V1.8:
#   - 後段patchで _board_guard が3引数版に戻り、
#     final_entry_safety_guard が4引数呼びで TypeError になる問題を防ぐため、
#     final_board_guard_signature_compat_patch を常時installする。
# ============================================================
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

VERSION = "V1.8-DISABLED-SAFE-BOARD-REQUIRED-SIG-COMPAT"
_INSTALLED = False


def _set_safe_defaults() -> None:
    """他patchから呼ばれても板なし発注を復活させない。"""
    os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK_FORCE", "1")
    os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD_FORCE", "0")
    os.environ["ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD"] = os.getenv("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_FORCE", "0")
    os.environ["ENTRY_SUMMARY_AI_FAST_BOARDLESS_ORDER"] = os.getenv("ENTRY_SUMMARY_AI_FAST_BOARDLESS_ORDER_FORCE", "0")
    os.environ.setdefault("ENTRY_BOARD_MISSING_POP_PENDING", "0")
    os.environ.setdefault("ENTRY_BOARD_MISSING_RETRYABLE", "1")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_COUNT", "0")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_COUNT", "0")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_WAIT_SEC", "0.05")


def _restore_final_guard_board_guard() -> bool:
    """
    既に旧V1.6が同一プロセス内で board guard を上書き済みの場合だけ、
    final_entry_safety_guard v10 の _board_guard へ戻す。
    新規プロセスでは通常何もしない。
    """
    try:
        import core.startup.final_entry_safety_guard_patch as fsg
        cur = getattr(fsg, "_board_guard", None)
        if getattr(cur, "_board_missing_failopen_v16", False):
            # final_entry_safety_guard_patch.py 由来の元関数があれば戻す。
            old = getattr(cur, "_original_board_guard", None)
            if callable(old):
                fsg._board_guard = old
                fsg._patched_board_guard = old
                logger.warning("[BOARD MISSING FAILOPEN] restored original final guard board guard version=%s", VERSION)
                return True
        return True
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] restore final guard failed")
        return False


def _install_board_guard_signature_compat() -> bool:
    try:
        from core.startup import final_board_guard_signature_compat_patch as sig
        fn = getattr(sig, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[BOARD MISSING FAILOPEN] board_guard_signature_compat installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] board_guard_signature_compat install failed")
        return False


def _patch_entry_order_builder_safe() -> bool:
    """entry_order_builder側も板なしfast fallbackへ変更しない。"""
    try:
        import trading.handlers.entry_order_builder as eob
        setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", True)
        return True
    except Exception:
        logger.debug("[BOARD MISSING FAILOPEN] entry_order_builder safe patch skipped", exc_info=True)
        return False


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
    _set_safe_defaults()
    guard_ok = _restore_final_guard_board_guard()
    sig_ok = _install_board_guard_signature_compat()
    order_ok = _patch_entry_order_builder_safe()
    lock_retry_ok = _install_summary_ai_lock_retry()
    stale_rescue_ok = _install_summary_entry_stale_rescue()
    _INSTALLED = True
    logger.warning(
        "[BOARD MISSING FAILOPEN] disabled safe install=%s guard_ok=%s sig_ok=%s order_safe=%s lock_retry_ok=%s stale_rescue_ok=%s hard_block=%s allow_without_board=%s summary_ai_allow=%s fast_boardless=%s version=%s",
        _INSTALLED,
        guard_ok,
        sig_ok,
        order_ok,
        lock_retry_ok,
        stale_rescue_ok,
        os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK"),
        os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
        os.getenv("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD"),
        os.getenv("ENTRY_SUMMARY_AI_FAST_BOARDLESS_ORDER"),
        VERSION,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD MISSING FAILOPEN] auto install failed")


__all__ = ["VERSION", "install"]
