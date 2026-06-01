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
_install("RANKING_ENTRY_FAST_BUDGET_OVERRIDE", "core.startup.ranking_entry_fast_budget_override_patch")
_install("TONOSAMA_RECENT_3M5M_FAILOPEN", "core.startup.tonosama_recent3m5m_failopen_patch")
_install("TONOSAMA_FAILOPEN_DIRECTION_RESCUE", "core.startup.tonosama_failopen_direction_rescue_patch")
