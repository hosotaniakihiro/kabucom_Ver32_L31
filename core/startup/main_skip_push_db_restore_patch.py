# ============================================================
# File   : core/startup/main_skip_push_db_restore_patch.py
# Version: V2-MAIN-SKIP-PUSH-DB-RESTORE-AND-PUSH-STACK
# ------------------------------------------------------------
# Purpose:
#   main.py 起動時に、NAS上の pushYYYYMMDD.db を同期的に直読み復元しない。
#   さらに main.py 側の PUSH storage writer / PUSH symbol bridge / PUSH stream
#   起動を既定でスキップし、0xC0000006 の発生面を最小化する。
#
# Reason:
#   2026-06-09 のログで safe migration と PUSH DB復元スキップは通過したが、
#   その後 main.py 側で PUSH DB writer が push20260609.db に接続し、
#   symbol bootstrap / position sync 後に Python例外ではなく Windows 0xC0000006 で
#   プロセス終了した。
#
# Policy:
#   - main.py は起動継続を最優先する。
#   - DB作成 / PUSH保存 / PUSH登録 / PUSH受信は main_database.py 側に寄せる。
#   - main.py 側でPUSHスタックを戻したい場合だけ
#       AUTOSTOCK_MAIN_SKIP_PUSH_STACK=0
#       AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE=0
#     を明示する。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_BOOTSTRAP_PUSH = None
_ORIGINAL_START_PUSH_STACK = None
_ORIGINAL_START_PUSH_STORAGE_SAFE = None
_ORIGINAL_START_PUSH_STREAM_EARLY_SAFE = None
_ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _is_main_py_process() -> bool:
    try:
        return Path(sys.argv[0]).name.lower() == "main.py"
    except Exception:
        return False


def _should_skip_db_restore() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE", True)


def _should_skip_push_stack() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_PUSH_STACK", True)


def _set_empty_push_df(reason: str, push_dir=None) -> None:
    try:
        from global_state import global_data
    except Exception:
        return

    empty = pd.DataFrame()
    try:
        global_data.push_df = empty
    except Exception:
        pass
    try:
        global_data.set_push_df(empty)
    except Exception:
        pass
    try:
        global_data.push_bootstrap_skipped = True
        global_data.push_bootstrap_skip_reason = reason
        global_data.push_bootstrap_skipped_by_patch = True
        global_data.push_bootstrap_push_dir = str(push_dir or "")
        global_data.push_bootstrap_rows = 0
        global_data.push_bootstrap_raw_rows = 0
    except Exception:
        pass


def _mark_push_stack_skipped(reason: str) -> None:
    try:
        from global_state import global_data
    except Exception:
        return

    try:
        global_data.push_writer_running = False
        global_data.push_storage_running = False
        global_data.push_storage_skipped_external = True
        global_data.push_stream_running = False
        global_data.push_stream_memory_only = False
        global_data.push_stream_skipped_external = True
        global_data.push_symbol_bridge_installed = False
        global_data.push_symbol_bridge_count = 0
        global_data.push_symbol_bridge_symbols = []
        global_data.push_collection_skipped_external = True
        global_data.push_collection_memory_merge_mode = False
        global_data.main_push_stack_skipped = True
        global_data.main_push_stack_skip_reason = reason
    except Exception:
        pass


def _patched_bootstrap_push(push_dir):
    if _should_skip_db_restore():
        _set_empty_push_df("main_skip_push_db_restore", push_dir=push_dir)
        logger.warning(
            "[MAIN SKIP PUSH DB RESTORE] skipped core.startup.push_bootstrap.bootstrap_push "
            "in main.py to avoid NAS SQLite direct-read 0xC0000006 push_dir=%s. "
            "Set AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE=0 to restore legacy behavior.",
            push_dir,
        )
        return None

    if callable(_ORIGINAL_BOOTSTRAP_PUSH):
        return _ORIGINAL_BOOTSTRAP_PUSH(push_dir)
    return None


def _patched_start_push_stack_before_scheduler():
    if _should_skip_push_stack():
        _set_empty_push_df("main_skip_push_stack", push_dir=None)
        _mark_push_stack_skipped("main_skip_push_stack")
        logger.warning(
            "[MAIN SKIP PUSH STACK] skipped full PUSH startup stack in main.py "
            "to avoid NAS SQLite writer/reader 0xC0000006. "
            "main_database.py handles PUSH storage/register/receive. "
            "Set AUTOSTOCK_MAIN_SKIP_PUSH_STACK=0 to restore legacy behavior."
        )
        return []

    if callable(_ORIGINAL_START_PUSH_STACK):
        return _ORIGINAL_START_PUSH_STACK()
    return []


def _patched_start_push_storage_safe():
    if _should_skip_push_stack():
        _mark_push_stack_skipped("main_skip_push_storage")
        logger.warning(
            "[MAIN SKIP PUSH STACK] skipped push storage writer in main.py. "
            "main_database.py handles push DB writer."
        )
        return None
    if callable(_ORIGINAL_START_PUSH_STORAGE_SAFE):
        return _ORIGINAL_START_PUSH_STORAGE_SAFE()
    return None


def _patched_start_push_stream_early_safe():
    if _should_skip_push_stack():
        _mark_push_stack_skipped("main_skip_push_stream_early")
        logger.warning(
            "[MAIN SKIP PUSH STACK] skipped early push stream in main.py. "
            "main_database.py handles PUSH receive/register."
        )
        return False
    if callable(_ORIGINAL_START_PUSH_STREAM_EARLY_SAFE):
        return _ORIGINAL_START_PUSH_STREAM_EARLY_SAFE()
    return False


def _patched_start_push_stream_fallback_safe():
    if _should_skip_push_stack():
        _mark_push_stack_skipped("main_skip_push_stream_fallback")
        logger.warning(
            "[MAIN SKIP PUSH STACK] skipped fallback push stream in main.py. "
            "main_database.py handles PUSH receive/register."
        )
        return False
    if callable(_ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE):
        return _ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE()
    return False


def install() -> bool:
    global _INSTALLED
    global _ORIGINAL_BOOTSTRAP_PUSH
    global _ORIGINAL_START_PUSH_STACK
    global _ORIGINAL_START_PUSH_STORAGE_SAFE
    global _ORIGINAL_START_PUSH_STREAM_EARLY_SAFE
    global _ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE

    try:
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE", "1")
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_PUSH_STACK", "1")
        os.environ.setdefault("PUSH_STREAM_DB_WRITE", "0")
        os.environ.setdefault("PUSH_STREAM_ORDER_BOOK_WRITE", "0")

        import core.startup.push_bootstrap as push_bootstrap_mod

        current = getattr(push_bootstrap_mod, "bootstrap_push", None)
        if getattr(current, "__name__", "") != "_patched_bootstrap_push":
            _ORIGINAL_BOOTSTRAP_PUSH = current
            push_bootstrap_mod.bootstrap_push = _patched_bootstrap_push

        try:
            import core.startup.push_startup as push_startup_mod

            # push_startup.py は `from core.startup.push_bootstrap import bootstrap_push` で
            # 関数参照を保持しているため、こちらも差し替える。
            push_startup_mod.bootstrap_push = _patched_bootstrap_push

            current_stack = getattr(push_startup_mod, "start_push_stack_before_scheduler", None)
            if getattr(current_stack, "__name__", "") != "_patched_start_push_stack_before_scheduler":
                _ORIGINAL_START_PUSH_STACK = current_stack
                push_startup_mod.start_push_stack_before_scheduler = _patched_start_push_stack_before_scheduler

            current_storage = getattr(push_startup_mod, "start_push_storage_safe", None)
            if getattr(current_storage, "__name__", "") != "_patched_start_push_storage_safe":
                _ORIGINAL_START_PUSH_STORAGE_SAFE = current_storage
                push_startup_mod.start_push_storage_safe = _patched_start_push_storage_safe

            current_early = getattr(push_startup_mod, "start_push_stream_early_safe", None)
            if getattr(current_early, "__name__", "") != "_patched_start_push_stream_early_safe":
                _ORIGINAL_START_PUSH_STREAM_EARLY_SAFE = current_early
                push_startup_mod.start_push_stream_early_safe = _patched_start_push_stream_early_safe

            current_fallback = getattr(push_startup_mod, "start_push_stream_fallback_safe", None)
            if getattr(current_fallback, "__name__", "") != "_patched_start_push_stream_fallback_safe":
                _ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE = current_fallback
                push_startup_mod.start_push_stream_fallback_safe = _patched_start_push_stream_fallback_safe

            # startup_orchestrator.py は import 時点で関数参照を保持している可能性があるため、
            # 既に読み込まれている場合はこちらも差し替える。
            try:
                import core.startup.startup_orchestrator as orchestrator_mod
                orchestrator_mod.start_push_stack_before_scheduler = _patched_start_push_stack_before_scheduler
                orchestrator_mod.start_push_stream_fallback_safe = _patched_start_push_stream_fallback_safe
            except Exception:
                logger.debug("[MAIN SKIP PUSH STACK] orchestrator patch skipped", exc_info=True)

        except Exception:
            logger.debug("[MAIN SKIP PUSH DB RESTORE] push_startup patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[MAIN SKIP PUSH DB RESTORE] installed enabled=%s main_py=%s push_stack_skip=%s",
            _should_skip_db_restore(),
            _is_main_py_process(),
            _should_skip_push_stack(),
        )
        return True
    except Exception:
        logger.exception("[MAIN SKIP PUSH DB RESTORE] install failed")
        return False


__all__ = ["install"]
