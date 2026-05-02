# ============================================================
# File   : core/startup/summary_startup.py
# Version: FINAL-PRODUCTION-REV23.0-SUMMARY-STARTUP
# ------------------------------------------------------------
# 【概要】
#   startup summary restore / summary fast boot / MTF history を担当
#
# 【機能】
#   ✔ startup_summary_restore
#   ✔ summary fast boot async
#   ✔ MTF history bootstrap
# ============================================================

from __future__ import annotations

import logging
from typing import Any

from global_state import global_data

from core.startup.summary_runtime import run_bootstrap_summary_fast_boot
from core.startup.mtf_history_bootstrap_runner import run_mtf_history_bootstrap_safe
from core.startup.startup_config import resolve_attr

logger = logging.getLogger(__name__)

VERSION = "FINAL-PRODUCTION-REV23.0-SUMMARY-STARTUP"


# ============================================================
# startup summary restore
# ============================================================

def run_startup_summary_restore_safe() -> Any:
    """
    起動時に summary DB / PUSH DB から必要最小限のデータを復元する。
    """
    logger.info("📊 startup summary restore start")

    try:
        global_data.startup_summary_restore_started = True
        global_data.startup_summary_restore_done = False
        global_data.startup_summary_restore_failed = False
        global_data.startup_summary_restore_result = None
    except Exception:
        pass

    try:
        restore_fn = resolve_attr(
            "core.startup.startup_summary_restore",
            "restore_startup_summary_minimal_tail",
        )

        if not callable(restore_fn):
            logger.warning(
                "⚠ startup summary restore function not found. "
                "Create core/startup/startup_summary_restore.py with "
                "restore_startup_summary_minimal_tail()."
            )

            try:
                global_data.startup_summary_restore_failed = True
                global_data.startup_summary_restore_result = {
                    "ok": False,
                    "message": "restore_startup_summary_minimal_tail not found",
                }
            except Exception:
                pass

            return None

        result = restore_fn(
            intervals=(1, 3, 5),
            display=True,
            save_missing=True,
            tail_rows=100,
            one_min_lookback_minutes=15,
        )

        ok = bool(getattr(result, "ok", False))
        msg = str(getattr(result, "message", ""))

        try:
            global_data.startup_summary_restore_done = ok
            global_data.startup_summary_restore_failed = not ok
            global_data.startup_summary_restore_result = result
        except Exception:
            pass

        logger.info(
            "✅ startup summary restore result "
            "ok=%s msg=%s "
            "summary_db=%s push_db=%s "
            "1min_rows=%s push_rows=%s "
            "existing3=%s existing5=%s "
            "new3=%s new5=%s "
            "saved3=%s saved5=%s "
            "load_from=%s",
            ok,
            msg,
            getattr(result, "summary_db", None),
            getattr(result, "push_db", None),
            getattr(result, "loaded_1min_rows", None),
            getattr(result, "loaded_push_rows", None),
            getattr(result, "existing_3min_rows", None),
            getattr(result, "existing_5min_rows", None),
            getattr(result, "new_3min_rows", None),
            getattr(result, "new_5min_rows", None),
            getattr(result, "saved_3min_rows", None),
            getattr(result, "saved_5min_rows", None),
            getattr(result, "one_min_load_from", None),
        )

        if not ok:
            logger.warning(
                "⚠ startup summary restore completed but ok=False. "
                "summary async bootstrap will continue later."
            )

        return result

    except Exception as e:
        try:
            global_data.startup_summary_restore_done = False
            global_data.startup_summary_restore_failed = True
            global_data.startup_summary_restore_result = {
                "ok": False,
                "message": str(e),
            }
        except Exception:
            pass

        logger.exception("❌ startup summary restore failed")
        return None


# ============================================================
# summary fast boot / MTF
# ============================================================

def start_summary_fast_boot_safe() -> None:
    """
    summary bootstrap は起動を止めず background で進める。
    """
    try:
        run_bootstrap_summary_fast_boot(force_sync=False)
    except Exception:
        logger.exception("❌ Summary bootstrap fast-boot start failed")


def run_mtf_history_bootstrap_startup_safe(*, market_open_now: bool) -> None:
    """
    scheduler / realtime entry より前後で使う MTF history bootstrap。
    """
    try:
        run_mtf_history_bootstrap_safe(market_open_now=market_open_now)
    except Exception:
        logger.exception("❌ MTF history bootstrap failed")


def start_summary_stack_after_scheduler(*, market_open_now: bool) -> None:
    """
    startup summary restore / summary fast boot / MTF history を実行。
    """
    run_startup_summary_restore_safe()
    start_summary_fast_boot_safe()
    run_mtf_history_bootstrap_startup_safe(market_open_now=market_open_now)


__all__ = [
    "VERSION",
    "run_startup_summary_restore_safe",
    "start_summary_fast_boot_safe",
    "run_mtf_history_bootstrap_startup_safe",
    "start_summary_stack_after_scheduler",
]