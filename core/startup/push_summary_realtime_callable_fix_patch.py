# ============================================================
# File   : core/startup/push_summary_realtime_callable_fix_patch.py
# Version: V1-PUSH-SUMMARY-REALTIME-CALLABLE-COMPAT
# ------------------------------------------------------------
# push_summary_realtime_patch.py の rebuild worker は
#   import trading.summary.engine.push_summary_engine as eng
#   eng.build_summary(...)
# を呼ぶ。
#
# ただし実行環境によっては、eng が module ではなく function として
# 解決され、以下で落ちることがある。
#   AttributeError: 'function' object has no attribute 'build_summary'
#
# この互換パッチでは、eng に build_summary 属性が無い場合だけ、
# scheduler_jobs.summary.runner_core.job_summary をラップした
# build_summary を付与する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _build_summary_compat(*, interval: int = 1, display: bool = False, now: Any = None, run_entry: bool = False, **kwargs):
    from scheduler_jobs.summary.runner_core import job_summary

    n = now
    if n is None:
        n = dt.datetime.now().replace(second=0, microsecond=0)
    return job_summary(int(interval), display=bool(display), now=n, run_entry=bool(run_entry))


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.summary.engine.push_summary_engine as eng

        existing = getattr(eng, "build_summary", None)
        if callable(existing):
            logger.warning("[PUSH SUMMARY REALTIME CALLABLE FIX] skipped build_summary already exists target=%s", type(eng).__name__)
            _INSTALLED = True
            return True

        setattr(eng, "build_summary", _build_summary_compat)
        _INSTALLED = True
        logger.warning(
            "[PUSH SUMMARY REALTIME CALLABLE FIX] installed build_summary compat target=%s callable=%s",
            type(eng).__name__,
            callable(eng),
        )
        return True
    except Exception as e:
        logger.exception("[PUSH SUMMARY REALTIME CALLABLE FIX] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[PUSH SUMMARY REALTIME CALLABLE FIX] auto install failed err=%s", e)


__all__ = ["install"]
