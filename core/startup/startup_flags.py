# ============================================================
# File   : core/startup/startup_flags.py
# Version: REV1.0-STARTUP-FLAGS
# ------------------------------------------------------------
# 【概要】
#   startup 系 bootstrap flags を global_data へ反映する小モジュール
#
# 【主な機能】
#   - summary unique index bootstrap flags
#   - MTF history bootstrap flags
# ============================================================

from __future__ import annotations

from global_state import global_data


def set_summary_unique_index_bootstrap_flags(
    *,
    started=None,
    done=None,
    failed=None,
    results=None,
) -> None:
    try:
        if started is not None:
            global_data.summary_unique_index_bootstrap_started = bool(started)
        if done is not None:
            global_data.summary_unique_index_bootstrap_done = bool(done)
        if failed is not None:
            global_data.summary_unique_index_bootstrap_failed = bool(failed)
        if results is not None:
            global_data.summary_unique_index_bootstrap_results = results
    except Exception:
        pass


def set_mtf_history_bootstrap_flags(
    *,
    started=None,
    done=None,
    failed=None,
    results=None,
) -> None:
    try:
        if started is not None:
            global_data.mtf_history_bootstrap_started = bool(started)
        if done is not None:
            global_data.mtf_history_bootstrap_done = bool(done)
        if failed is not None:
            global_data.mtf_history_bootstrap_failed = bool(failed)
        if results is not None:
            global_data.mtf_history_bootstrap_results = results
    except Exception:
        pass


def reset_startup_flags() -> None:
    try:
        global_data.summary_bootstrap_started = False
        global_data.summary_bootstrap_done = False
        global_data.summary_bootstrap_failed = False
    except Exception:
        pass

    set_summary_unique_index_bootstrap_flags(
        started=False,
        done=False,
        failed=False,
        results=None,
    )

    set_mtf_history_bootstrap_flags(
        started=False,
        done=False,
        failed=False,
        results=None,
    )


__all__ = [
    "set_summary_unique_index_bootstrap_flags",
    "set_mtf_history_bootstrap_flags",
    "reset_startup_flags",
]