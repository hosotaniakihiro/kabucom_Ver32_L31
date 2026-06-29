# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_entry_pending_age_relax_patch.py
# Version: V1-SUMMARY-PENDING-AGE-900S
# ------------------------------------------------------------
# Purpose:
#   2026-06-29 logs showed SUMMARY_AI approved rows reached pending
#   registration, but were skipped as stale because the default age limit was
#   180s.  In live operation, PUSH/summary/display/AI dispatch can lag several
#   minutes, so 180s is too strict and causes no_pending_registered.
#
#   This patch widens only the pending-registration stale window to 900s.
#   It does not disable stale protection and does not bypass final order guards.
# ============================================================
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-PENDING-AGE-900S"
_INSTALLED = False


def _safe_float(v, default: float) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _raise_min_env(name: str, minimum: float) -> tuple[str | None, str]:
    old = os.environ.get(name)
    cur = _safe_float(old, minimum)
    if old is None or cur < minimum:
        os.environ[name] = str(int(minimum) if float(minimum).is_integer() else minimum)
    return old, os.environ.get(name, "")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        minimum = _safe_float(os.environ.get("SUMMARY_ENTRY_PENDING_RELAX_MIN_AGE_SEC"), 900.0)
        changed = {}
        for name in (
            "SUMMARY_ENTRY_PENDING_MAX_AGE_SEC",
            "SUMMARY_AI_PENDING_MAX_AGE_SEC",
            "SUMMARY_AI_MAX_CANDIDATE_AGE_SEC",
            "ENTRY_CANDIDATE_MAX_AGE_SEC",
        ):
            old, new = _raise_min_env(name, minimum)
            changed[name] = {"old": old, "new": new}

        # The existing pending fix reads these env values dynamically, so no
        # rewrap is required.  Recalling install is harmless if it is not yet installed.
        try:
            from . import summary_entry_pending_existing_fix_patch
            summary_entry_pending_existing_fix_patch.install()
        except Exception:
            logger.debug("[SUMMARY PENDING AGE RELAX] pending existing fix install check skipped", exc_info=True)

        _INSTALLED = True
        logger.warning("[SUMMARY PENDING AGE RELAX] installed version=%s min_age=%s changed=%s", VERSION, minimum, changed)
        return True
    except Exception:
        logger.exception("[SUMMARY PENDING AGE RELAX] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY PENDING AGE RELAX] auto install failed")

__all__ = ["VERSION", "install"]
