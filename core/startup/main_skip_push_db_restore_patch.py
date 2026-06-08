# ============================================================
# File   : core/startup/main_skip_push_db_restore_patch.py
# Version: V1-MAIN-SKIP-PUSH-DB-RESTORE
# ------------------------------------------------------------
# Purpose:
#   main.py 起動時に、NAS上の pushYYYYMMDD.db を同期的に直読み復元しない。
#
# Reason:
#   2026-06-09 のログで safe migration は通過したが、
#   core.startup.push_bootstrap が push20260609.db を1639行復元し、
#   normalize/time parse の直後に Python例外ではなく Windows 0xC0000006 で
#   プロセス終了した。
#
# Policy:
#   - main.py は起動継続を最優先する。
#   - PUSH DB復元は main_database.py / writer / WebSocket のリアルタイム更新に任せる。
#   - 戻したい場合だけ AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE=0 を指定する。
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


def _should_skip() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE", True)


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


def _patched_bootstrap_push(push_dir):
    if _should_skip():
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


def install() -> bool:
    global _INSTALLED, _ORIGINAL_BOOTSTRAP_PUSH

    try:
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE", "1")

        import core.startup.push_bootstrap as push_bootstrap_mod

        current = getattr(push_bootstrap_mod, "bootstrap_push", None)
        if getattr(current, "__name__", "") != "_patched_bootstrap_push":
            _ORIGINAL_BOOTSTRAP_PUSH = current
            push_bootstrap_mod.bootstrap_push = _patched_bootstrap_push

        # push_startup.py は `from core.startup.push_bootstrap import bootstrap_push` で
        # 関数参照を保持しているため、こちらも差し替える。
        try:
            import core.startup.push_startup as push_startup_mod
            push_startup_mod.bootstrap_push = _patched_bootstrap_push
        except Exception:
            logger.debug("[MAIN SKIP PUSH DB RESTORE] push_startup patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[MAIN SKIP PUSH DB RESTORE] installed enabled=%s main_py=%s",
            _should_skip(),
            _is_main_py_process(),
        )
        return True
    except Exception:
        logger.exception("[MAIN SKIP PUSH DB RESTORE] install failed")
        return False


__all__ = ["install"]
