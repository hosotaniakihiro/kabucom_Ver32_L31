# -*- coding: utf-8 -*-
"""
Compatibility installer for centralized runtime environment defaults.

V22 fixes an ImportError seen in data collector children:
    cannot import name 'apply_site_defaults' from core.startup.runtime_env_defaults
The defaults module currently exposes grouped apply_*_defaults functions, so this
module now builds apply_site_defaults/apply_user_defaults locally when the old
aggregate helpers are not present.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Dict

from .runtime_env_default_registry import SITE_GROUP_ORDER, USER_GROUP_ORDER
from .runtime_env_default_registry import VERSION as REGISTRY_VERSION
from .runtime_settings_ini_loader import VERSION as SETTINGS_INI_VERSION
from .runtime_settings_ini_loader import load_settings_ini
from . import runtime_env_defaults as _defaults

logger = logging.getLogger(__name__)
VERSION = "REV22-RUNTIME-ENV-DEFAULTS-PATCH-COMPAT-GROUPED-DEFAULTS"
DEFAULTS_VERSION = getattr(_defaults, "VERSION", "unknown")
env_bool = getattr(_defaults, "env_bool")
_INSTALLED = False


_GROUP_APPLIERS: dict[str, Callable[..., Dict[str, str]]] = {
    "push": getattr(_defaults, "apply_push_defaults"),
    "rescue": getattr(_defaults, "apply_rescue_defaults"),
    "db": getattr(_defaults, "apply_db_defaults"),
    "helper": getattr(_defaults, "apply_helper_defaults"),
    "main_restore": getattr(_defaults, "apply_main_restore_defaults"),
    "ranking_entry": getattr(_defaults, "apply_ranking_entry_defaults"),
    "tonosama": getattr(_defaults, "apply_tonosama_defaults"),
    "entry": getattr(_defaults, "apply_entry_defaults"),
    "summary_yahoo": getattr(_defaults, "apply_summary_yahoo_defaults"),
}


def _apply_groups(order: tuple[str, ...], *, context: str) -> Dict[str, str]:
    applied: Dict[str, str] = {}
    for name in order:
        fn = _GROUP_APPLIERS.get(name)
        if not callable(fn):
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] group applier missing name=%s context=%s", name, context)
            continue
        try:
            applied.update(fn())
        except Exception:
            logger.exception("[RUNTIME ENV DEFAULTS PATCH] group apply failed name=%s context=%s", name, context)
    return applied


def apply_site_defaults(*, context: str = "unknown") -> Dict[str, str]:
    old = getattr(_defaults, "apply_site_defaults", None)
    if callable(old):
        return old(context=context)
    return _apply_groups(SITE_GROUP_ORDER, context=context)


def apply_user_defaults(*, context: str = "unknown") -> Dict[str, str]:
    old = getattr(_defaults, "apply_user_defaults", None)
    if callable(old):
        return old(context=context)
    return _apply_groups(USER_GROUP_ORDER, context=context)


def _argv_context() -> str:
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text:
            return "main_database"
        if "main.py" in text:
            return "main"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _safe_install(label: str, context: str, allowed: set[str], env_disable: str, module_name: str) -> bool:
    try:
        if context not in allowed:
            return False
        if os.environ.get(env_disable, "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] %s disabled by env", label)
            return False
        mod = __import__(f"core.startup.{module_name}", fromlist=["install"])
        fn = getattr(mod, "install", None)
        return bool(fn()) if callable(fn) else False
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] %s install failed", label)
        return False


def _install_entry_fire_rescue(context: str) -> bool:
    return _safe_install("entry fire rescue", context, {"main", "helper"}, "DISABLE_ENTRY_FIRE_RESCUE_PATCH", "entry_fire_rescue_runtime_patch")


def _install_summary_pending_stale_guard(context: str) -> bool:
    try:
        if context not in {"main", "helper"}:
            return False
        if os.environ.get("DISABLE_SUMMARY_ENTRY_PENDING_FIX_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] summary pending stale guard disabled by env")
            return False
        os.environ.setdefault("SUMMARY_SKIP_STALE_PENDING_ENABLED", "1")
        os.environ.setdefault("SUMMARY_SKIP_MISSING_DATETIME_PENDING", "1")
        os.environ.setdefault("SUMMARY_ENTRY_PENDING_MAX_AGE_SEC", "180")
        from . import summary_entry_pending_existing_fix_patch
        return bool(summary_entry_pending_existing_fix_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] summary pending stale guard install failed")
        return False


def _install_summary_db_realtime_priority(context: str) -> bool:
    return _safe_install("summary DB realtime priority", context, {"main_database", "helper"}, "DISABLE_SUMMARY_DB_REALTIME_PRIORITY_PATCH", "summary_db_realtime_priority_patch")


def _install_ranking_entry_runtime_rescue(context: str) -> bool:
    return _safe_install("ranking entry runtime rescue", context, {"main", "helper"}, "DISABLE_RANKING_ENTRY_RUNTIME_RESCUE_PATCH", "ranking_entry_runtime_rescue_patch")


def _install_low_volatility_entry_guard(context: str) -> bool:
    try:
        if context not in {"main", "helper"}:
            return False
        if os.environ.get("DISABLE_LOW_VOLATILITY_ENTRY_GUARD", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] low volatility entry guard disabled by env")
            return False
        from . import low_volatility_entry_guard_patch
        base_ok = bool(low_volatility_entry_guard_patch.install())
        strict_ok = False
        try:
            from . import low_volatility_entry_guard_strict_patch
            strict_ok = bool(low_volatility_entry_guard_strict_patch.install())
        except Exception:
            logger.exception("[RUNTIME ENV DEFAULTS PATCH] strict low volatility guard install failed")
        return bool(base_ok or strict_ok)
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] low volatility entry guard install failed")
        return False


def _install_push_registration_recovery(context: str) -> bool:
    return _safe_install("push register recovery", context, {"main_database", "helper", "main"}, "DISABLE_PUSH_REGISTER_RECOVERY_PATCH", "push_registration_recovery_patch")


def _install_common_day_position_guard(context: str) -> bool:
    return _safe_install("common day position guard", context, {"main", "helper"}, "DISABLE_COMMON_ENTRY_DAY_POSITION_GUARD", "common_entry_day_position_guard_patch")


def _install_strict_final_liquidity_guard(context: str) -> bool:
    return _safe_install("strict final liquidity guard", context, {"main", "helper"}, "DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PATCH", "entry_handler_strict_recent_liquidity_patch")


def _install_tonosama_exit_source_infer(context: str) -> bool:
    return _safe_install("tonosama exit infer", context, {"main", "helper"}, "DISABLE_TONOSAMA_EXIT_SOURCE_INFER_PATCH", "tonosama_exit_source_infer_patch")


def _install_tonosama_pending_candidate_audit(context: str) -> bool:
    return _safe_install("tonosama pending candidate audit", context, {"main", "helper"}, "DISABLE_TONOSAMA_PENDING_CANDIDATE_AUDIT_PATCH", "tonosama_pending_candidate_audit_patch")


def _install_daytrade_credit_force_close(context: str) -> bool:
    return _safe_install("daytrade credit force close", context, {"main", "helper"}, "DISABLE_DAYTRADE_CREDIT_FORCE_CLOSE_PATCH", "daytrade_credit_force_close_patch")


def _install_database_owner(context: str) -> bool:
    return _safe_install("database owner", context, {"main", "helper", "main_database"}, "DISABLE_DATABASE_OWNER_RUNTIME_PATCH", "database_owner_runtime_patch")


def _install_entry_count_unblock(context: str) -> bool:
    return _safe_install("entry count unblock", context, {"main", "helper", "main_database"}, "DISABLE_ENTRY_COUNT_UNBLOCK_PATCH", "entry_count_unblock_runtime_patch")


def _install_full_pipeline_stability(context: str) -> bool:
    return _safe_install("full pipeline stability", context, {"main", "helper", "main_database"}, "DISABLE_FULL_PIPELINE_STABILITY_PATCH", "full_pipeline_stability_runtime_patch")


def _install_summary_db_lock_pressure(context: str) -> bool:
    return _safe_install("summary DB lock pressure", context, {"main", "helper", "main_database"}, "DISABLE_SUMMARY_DB_LOCK_PRESSURE_PATCH", "summary_db_lock_pressure_patch")


def _install_intraday_load_guard(context: str) -> bool:
    return _safe_install("intraday load guard", context, {"main", "helper", "main_database"}, "DISABLE_INTRADAY_LOAD_GUARD_PATCH", "intraday_load_guard_patch")


def _install_yahoo_parallel_empty_cooldown(context: str) -> bool:
    return _safe_install("yahoo parallel empty cooldown", context, {"main_database", "helper", "main"}, "DISABLE_YAHOO_PARALLEL_EMPTY_COOLDOWN_PATCH", "yahoo_parallel_empty_cooldown_patch")


def _install_ranking_legacy_inline_flush(context: str) -> bool:
    return _safe_install("ranking legacy inline flush", context, {"main_database", "helper", "main"}, "DISABLE_RANKING_LEGACY_INLINE_FLUSH_PATCH", "ranking_legacy_inline_flush_patch")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        context = _argv_context()
        settings_applied: Dict[str, str] = load_settings_ini(context=context)
        applied: Dict[str, str] = {}
        applied.update(apply_site_defaults(context=context))
        applied.update(apply_user_defaults(context=context))

        intraday_load_guard_ok = _install_intraday_load_guard(context)
        yahoo_parallel_empty_ok = _install_yahoo_parallel_empty_cooldown(context)
        ranking_legacy_inline_ok = _install_ranking_legacy_inline_flush(context)
        summary_lock_pressure_ok = _install_summary_db_lock_pressure(context)
        summary_db_realtime_ok = _install_summary_db_realtime_priority(context)
        database_owner_ok = _install_database_owner(context)
        full_pipeline_ok = _install_full_pipeline_stability(context)
        entry_count_unblock_ok = _install_entry_count_unblock(context)
        summary_pending_stale_ok = _install_summary_pending_stale_guard(context)
        entry_fire_rescue_ok = _install_entry_fire_rescue(context)
        ranking_entry_rescue_ok = _install_ranking_entry_runtime_rescue(context)
        low_vol_guard_ok = _install_low_volatility_entry_guard(context)
        push_register_recovery_ok = _install_push_registration_recovery(context)
        day_position_guard_ok = _install_common_day_position_guard(context)
        strict_final_liq_ok = _install_strict_final_liquidity_guard(context)
        tonosama_exit_infer_ok = _install_tonosama_exit_source_infer(context)
        tonosama_pending_audit_ok = _install_tonosama_pending_candidate_audit(context)
        daytrade_credit_ok = _install_daytrade_credit_force_close(context)
        _INSTALLED = True
        logger.warning(
            "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s settings_ini=%s settings_applied=%s builtins_applied=%s context=%s site_groups=%s user_groups=%s intraday_load_guard=%s yahoo_parallel_empty=%s ranking_legacy_inline=%s summary_lock_pressure=%s summary_db_realtime=%s database_owner=%s full_pipeline=%s rescue=%s ranking_rescue=%s tonosama_rescue=%s entry_count_unblock=%s summary_pending_stale=%s entry_fire_rescue=%s ranking_entry_rescue=%s low_vol_guard=%s push_register_recovery=%s day_position_guard=%s strict_final_liq=%s tonosama_exit_infer=%s tonosama_pending_audit=%s daytrade_credit=%s verbose=%s",
            VERSION,
            DEFAULTS_VERSION,
            REGISTRY_VERSION,
            SETTINGS_INI_VERSION,
            len(settings_applied),
            len(applied),
            context,
            ",".join(SITE_GROUP_ORDER),
            ",".join(USER_GROUP_ORDER),
            intraday_load_guard_ok,
            yahoo_parallel_empty_ok,
            ranking_legacy_inline_ok,
            summary_lock_pressure_ok,
            summary_db_realtime_ok,
            database_owner_ok,
            full_pipeline_ok,
            os.environ.get("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES"),
            os.environ.get("USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES"),
            os.environ.get("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES"),
            entry_count_unblock_ok,
            summary_pending_stale_ok,
            entry_fire_rescue_ok,
            ranking_entry_rescue_ok,
            low_vol_guard_ok,
            push_register_recovery_ok,
            day_position_guard_ok,
            strict_final_liq_ok,
            tonosama_exit_infer_ok,
            tonosama_pending_audit_ok,
            daytrade_credit_ok,
            env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False),
        )
        return True
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] install failed")
        return False


__all__ = ["VERSION", "install", "apply_site_defaults", "apply_user_defaults"]
