# ============================================================
# File   : core/startup/summary_scheduler_timeout_patch.py
# Version: PRODUCTION-STABLE-REV1-SUMMARY-SCHEDULER-TIMEOUT-PATCH
# ------------------------------------------------------------
# Purpose:
#   - PUSH 1m summary が 35秒 timeout で CALL timeout になり、
#     entry 実行前に scheduler 側で失敗扱いになる問題を緩和する。
#   - 既存 scheduler.py は環境変数で timeout を読めるため、
#     起動時に未設定なら安全側の既定値を入れるだけにする。
#
# Defaults:
#   SUMMARY_CHILD_JOB_TIMEOUT_SEC  : 90秒
#   SUMMARY_PARENT_TICK_TIMEOUT_SEC: 120秒
#   SUMMARY_PUSH_FALLBACK_STALE_SEC: 300秒
#
# Notes:
#   - すでに環境変数が設定されている場合は上書きしない。
#   - コード側の scheduler.py を大きく書き換えずに運用安全性を上げる。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "SUMMARY_CHILD_JOB_TIMEOUT_SEC": "90",
    "SUMMARY_PARENT_TICK_TIMEOUT_SEC": "120",
    "SUMMARY_PUSH_FALLBACK_STALE_SEC": "300",
}

_INSTALLED = False


def _is_blank(v: object) -> bool:
    try:
        return v is None or str(v).strip() == ""
    except Exception:
        return True


def install_summary_scheduler_timeout_patch() -> None:
    """
    summary.scheduler が timeout を読む前に既定値を補強する。
    """
    global _INSTALLED
    if _INSTALLED:
        return

    applied: dict[str, str] = {}
    kept: dict[str, str] = {}

    for key, default_value in _DEFAULTS.items():
        cur = os.environ.get(key)
        if _is_blank(cur):
            os.environ[key] = str(default_value)
            applied[key] = str(default_value)
        else:
            kept[key] = str(cur)

    _INSTALLED = True

    logger.warning(
        "[SUMMARY SCHEDULER TIMEOUT PATCH] installed applied=%s kept=%s",
        applied,
        kept,
    )


__all__ = ["install_summary_scheduler_timeout_patch"]
