# ============================================================
# File   : trading/summary/summary_display.py
# Version: Ver5-PRODUCTION-SUMMARY-DISPLAY-ENTRYPOINT-FINAL
# ------------------------------------------------------------
# ✔ summary display entrypoint
# ✔ display engine wrapper
# ✔ runtime crash isolation
# ✔ optional enable/disable
# ✔ real-time trading safe
# ✔ institutional wrapper module
# ✔ kwargs pass-through
# ✔ bool result
# ✔ lazy import safe
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# lazy resolver
# ============================================================

def _resolve_run_summary_display():
    try:
        from trading.summary.display.summary_display_engine import run_summary_display
        if callable(run_summary_display):
            return run_summary_display
    except Exception:
        logger.exception("[SUMMARY DISPLAY] failed to import run_summary_display")
    return None


# ============================================================
# DISPLAY ENTRYPOINT
# ============================================================

def display_summary(*args: Any, **kwargs: Any) -> bool:
    try:
        runner = _resolve_run_summary_display()
        if not callable(runner):
            logger.warning("[SUMMARY DISPLAY] run_summary_display unavailable")
            return False

        runner(*args, **kwargs)
        return True

    except Exception:
        logger.exception("[SUMMARY DISPLAY] display failed")
        return False


# ============================================================
# OPTIONAL SAFE CALL
# ============================================================

def safe_display_summary(enable: bool = True, *args: Any, **kwargs: Any) -> bool:
    if not enable:
        logger.info("[SUMMARY DISPLAY] skipped: enable=False")
        return False

    try:
        return bool(display_summary(*args, **kwargs))
    except Exception:
        logger.exception("[SUMMARY DISPLAY] safe display failed")
        return False


__all__ = [
    "display_summary",
    "safe_display_summary",
]