# ============================================================
# File   : usercustomize.py
# Version: V2-AUTOINSTALL-SMALL-RUNTIME-PATCHES
# ------------------------------------------------------------
# Python site module imports usercustomize after sitecustomize when the
# project root is on sys.path. Keep this tiny and non-fatal.
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _install(label: str, module_name: str) -> None:
    try:
        mod = __import__(module_name, fromlist=["install"])
        fn = getattr(mod, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[USERCUSTOMIZE] %s auto install ok=%s", label, ok)
    except Exception:
        logger.exception("[USERCUSTOMIZE] %s auto install failed", label)


_install("TONOSAMA_LUNCH_REOPEN_RECENT", "core.startup.tonosama_lunch_reopen_recent_patch")
_install("YAHOO_COMPUTE_SCHEMA_NA_GUARD", "core.startup.yahoo_compute_schema_na_guard_patch")
