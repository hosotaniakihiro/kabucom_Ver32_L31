# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-RANKING-SUMMARY-FAST-ENTRY-ROUTE"
_INSTALLED = False


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return max(1, int(float(raw)))
    except Exception:
        return int(default)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        # The ranking summary build was taking several minutes in main.py,
        # then BG timeout/cooldown prevented the RANKING AI hook from running.
        # Keep the route live by using a short lookback for scheduled entry.
        os.environ.setdefault("RANKING_SUMMARY_BG_TIMEOUT_SEC", "180")
        os.environ.setdefault("RANKING_SUMMARY_BG_COOLDOWN_SEC", "30")
        os.environ.setdefault("RANKING_SUMMARY_INTERNAL_STALE_SEC", "240")
        os.environ.setdefault("RANKING_SUMMARY_BG_STALE_SEC", "240")
        os.environ.setdefault("RANKING_SUMMARY_ENTRY_LOOKBACK_MIN", "20")

        import scheduler_jobs.summary.ranking_summary_jobs as jobs
        cur = getattr(jobs, "job_ranking_summary_all", None)
        if not callable(cur) or getattr(cur, "_ranking_summary_fast_entry_v1", False):
            _INSTALLED = True
            return True

        @wraps(cur)
        def _wrapped_job_ranking_summary_all(*args: Any, **kwargs: Any):
            try:
                if kwargs.get("run_entry", True):
                    cap = _env_int("RANKING_SUMMARY_ENTRY_LOOKBACK_MIN", 20)
                    old = kwargs.get("lookback_minutes", 240)
                    try:
                        old_i = int(old)
                    except Exception:
                        old_i = 240
                    if old_i > cap:
                        kwargs["lookback_minutes"] = cap
                    logger.warning(
                        "[RANKING SUMMARY FAST ENTRY] lookback capped old=%s new=%s version=%s",
                        old,
                        kwargs.get("lookback_minutes"),
                        VERSION,
                    )
            except Exception:
                pass
            return cur(*args, **kwargs)

        _wrapped_job_ranking_summary_all._ranking_summary_fast_entry_v1 = True  # type: ignore[attr-defined]
        _wrapped_job_ranking_summary_all._original = cur  # type: ignore[attr-defined]
        jobs.job_ranking_summary_all = _wrapped_job_ranking_summary_all
        jobs.run_ranking_summary_job = _wrapped_job_ranking_summary_all
        jobs.job_summary_ranking = _wrapped_job_ranking_summary_all
        _INSTALLED = True
        logger.warning("[RANKING SUMMARY FAST ENTRY] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[RANKING SUMMARY FAST ENTRY] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING SUMMARY FAST ENTRY] auto install failed")


__all__ = ["install", "VERSION"]
