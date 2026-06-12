# ============================================================
# File   : core/startup/summary_mtf_catchup_owner_guard_patch.py
# Version: V1-SUMMARY-MTF-CATCHUP-OWNER-GUARD
# ------------------------------------------------------------
# sitecustomize.py imports summary_multiframe_startup_catchup_patch
# in every DB/data-collector process.  The original module starts a
# delayed background catchup thread at import/install time, which means
# push_receiver / yahoo_complement / summary_database / ranking_collector
# can all race to write summaryYYYYMMDD.db.
#
# This guard is intentionally installed from usercustomize after
# sitecustomize has scheduled the delayed thread.  The delayed thread
# looks up module globals when it wakes, so replacing _run_background and
# run_catchup here prevents non-owner child processes from doing the
# heavy MTF upsert.
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
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        return ""


def _is_owner_process() -> bool:
    """Default owner is main_database.py only."""
    if _env_bool("SUMMARY_MTF_CATCHUP_FORCE_THIS_PROCESS", False):
        return True
    argv = _argv_text()
    if "main_database.py" in argv:
        return True
    if _env_bool("SUMMARY_MTF_CATCHUP_ALLOW_SUMMARY_DATABASE_RUNNER", False) and "summary_database_runner.py" in argv:
        return True
    return False


def _install_owner_defaults(owner: bool) -> None:
    # Avoid child-process heavy startup upserts.  Owner gets longer retry instead of early skip.
    if owner:
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_UPSERT_RETRY_COUNT", "8")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_UPSERT_RETRY_SLEEP_SEC", "0.75")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_UPSERT_CHUNK_SIZE", "200")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_SQLITE_TIMEOUT", "45")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_BUSY_TIMEOUT_MS", "45000")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_SKIP_IF_BUSY", "0")
    else:
        os.environ.setdefault("SUMMARY_MTF_STARTUP_CATCHUP_ENABLED", "0")
        os.environ.setdefault("SUMMARY_MTF_INDICATOR_FILL_AFTER_CATCHUP", "0")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    owner = _is_owner_process()
    _install_owner_defaults(owner)
    try:
        import core.startup.summary_multiframe_startup_catchup_patch as mod

        # Keep original functions for owner, but prevent already-scheduled delayed threads in children.
        if not owner:
            def _skip_run_background(reason: str = "startup") -> None:
                try:
                    setattr(mod, "_RUNNING", False)
                except Exception:
                    pass
                logger.warning(
                    "[SUMMARY MTF OWNER GUARD] skipped non-owner background catchup reason=%s argv=%s",
                    reason,
                    sys.argv,
                )

            def _skip_run_catchup(*, reason: str = "manual") -> dict[str, Any]:
                try:
                    setattr(mod, "_RUNNING", False)
                except Exception:
                    pass
                logger.warning(
                    "[SUMMARY MTF OWNER GUARD] skipped non-owner run_catchup reason=%s argv=%s",
                    reason,
                    sys.argv,
                )
                return {"ok": True, "skipped": True, "reason": reason, "owner": False}

            mod._run_background = _skip_run_background  # type: ignore[attr-defined]
            mod.run_catchup = _skip_run_catchup  # type: ignore[attr-defined]
            try:
                setattr(mod, "_RUNNING", False)
            except Exception:
                pass

        _INSTALLED = True
        logger.warning(
            "[SUMMARY MTF OWNER GUARD] installed owner=%s argv0=%s retry=%s skip_if_busy=%s",
            owner,
            Path(sys.argv[0]).name if sys.argv else "",
            os.getenv("SUMMARY_MTF_CATCHUP_UPSERT_RETRY_COUNT"),
            os.getenv("SUMMARY_MTF_CATCHUP_SKIP_IF_BUSY"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MTF OWNER GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MTF OWNER GUARD] auto install failed")


__all__ = ["install"]
