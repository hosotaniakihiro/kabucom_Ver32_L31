# ============================================================
# File   : usercustomize.py
# Version: V1-AUTOINSTALL-TONOSAMA-LUNCH-REOPEN
# ------------------------------------------------------------
# Python site module imports usercustomize after sitecustomize when the
# project root is on sys.path.  Keep this tiny and non-fatal.
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from core.startup.tonosama_lunch_reopen_recent_patch import install
    ok = bool(install())
    logger.warning("[USERCUSTOMIZE] TONOSAMA_LUNCH_REOPEN_RECENT auto install ok=%s", ok)
except Exception:
    logger.exception("[USERCUSTOMIZE] TONOSAMA_LUNCH_REOPEN_RECENT auto install failed")
