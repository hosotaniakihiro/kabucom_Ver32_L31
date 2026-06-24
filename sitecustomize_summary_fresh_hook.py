# -*- coding: utf-8 -*-
"""Small import hook for summary fresh overwrite patch."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def install() -> bool:
    try:
        from core.startup.summary_fresh_overwrite_patch import install as _install
        ok = bool(_install())
        logger.warning("[SUMMARY FRESH HOOK] installed ok=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY FRESH HOOK] install failed")
        return False


try:
    install()
except Exception:
    pass
