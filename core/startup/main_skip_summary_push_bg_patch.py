# ============================================================
# File   : core/startup/main_skip_summary_push_bg_patch.py
# Version: V1-MAIN-SKIP-SUMMARY-PUSH-BG
# ------------------------------------------------------------
# main.py 起動時は PUSH DB / PUSH stack / PUSH fallback を止めても、
# summary_parallel_intervals_runtime_patch が PUSH 1m/3m/5m を
# background thread で起動し、runner が NAS DB/cache 読みに入る。
# Windows 0xC0000006 対策として main.py では PUSH summary BG も止める。
# main_database.py が PUSH受信・summary作成を担当する。
# ============================================================
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _is_main_py() -> bool:
    try:
        if _env_bool("AUTOSTOCK_MAIN_DATABASE_PROCESS", False):
            return False
        if _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False):
            return False
        argv0 = Path(str(sys.argv[0] or "")).name.lower()
        if argv0 == "main.py":
            return True
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return role in {"entry_only", "main_entry_only", "read_only", "no_save"} or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False)
    except Exception:
        return False


def _enabled() -> bool:
    return _env_bool("AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG", True) and _is_main_py()


def _install_summary_parallel_patch() -> bool:
    try:
        import core.startup.summary_parallel_intervals_runtime_patch as sp
    except Exception:
        logger.debug("[MAIN SKIP SUMMARY PUSH BG] summary_parallel module unavailable", exc_info=True)
        return False

    def _split_push_wait_and_bg_no_push(push_targets: list[int], *, in_session: bool) -> tuple[list[int], list[int]]:
        if _enabled():
            try:
                logger.warning(
                    "[MAIN SKIP SUMMARY PUSH BG] skip wait/bg push targets in main.py targets=%s in_session=%s",
                    push_targets,
                    in_session,
                )
            except Exception:
                pass
            return [], []
        try:
            orig = getattr(sp, "_ORIG_SPLIT_PUSH_WAIT_AND_BG", None)
            if callable(orig):
                return orig(push_targets, in_session=in_session)
        except Exception:
            pass
        return [], []

    def _submit_bg_push_interval_noop(*, interval: int, now: Any, display: bool, run_entry: bool) -> None:
        if _enabled():
            logger.warning(
                "[MAIN SKIP SUMMARY PUSH BG] skipped bg push interval=%s now=%s display=%s run_entry=%s",
                interval,
                now,
                display,
                run_entry,
            )
            return None
        try:
            orig = getattr(sp, "_ORIG_SUBMIT_BG_PUSH_INTERVAL", None)
            if callable(orig):
                return orig(interval=interval, now=now, display=display, run_entry=run_entry)
        except Exception:
            logger.exception("[MAIN SKIP SUMMARY PUSH BG] original bg submit failed")
        return None

    try:
        if not hasattr(sp, "_ORIG_SPLIT_PUSH_WAIT_AND_BG"):
            setattr(sp, "_ORIG_SPLIT_PUSH_WAIT_AND_BG", getattr(sp, "_split_push_wait_and_bg", None))
        if not hasattr(sp, "_ORIG_SUBMIT_BG_PUSH_INTERVAL"):
            setattr(sp, "_ORIG_SUBMIT_BG_PUSH_INTERVAL", getattr(sp, "_submit_bg_push_interval", None))
        sp._split_push_wait_and_bg = _split_push_wait_and_bg_no_push
        sp._submit_bg_push_interval = _submit_bg_push_interval_noop
        logger.warning("[MAIN SKIP SUMMARY PUSH BG] summary_parallel patched enabled=%s main_py=%s", _enabled(), _is_main_py())
        return True
    except Exception:
        logger.exception("[MAIN SKIP SUMMARY PUSH BG] summary_parallel patch failed")
        return False


def install() -> bool:
    global _INSTALLED
    try:
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG", "1")
        # 既存patchが main.py で BG を強制ONにしても、このpatchで最終的に空にする。
        ok = _install_summary_parallel_patch()
        _INSTALLED = bool(ok)
        logger.warning(
            "[MAIN SKIP SUMMARY PUSH BG] installed ok=%s enabled=%s main_py=%s",
            ok,
            _enabled(),
            _is_main_py(),
        )
        return bool(ok)
    except Exception:
        logger.exception("[MAIN SKIP SUMMARY PUSH BG] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[MAIN SKIP SUMMARY PUSH BG] auto install failed")


__all__ = ["install"]
