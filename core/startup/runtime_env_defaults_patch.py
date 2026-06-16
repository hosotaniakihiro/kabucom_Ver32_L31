# -*- coding: utf-8 -*-
"""
Compatibility installer for centralized runtime environment defaults.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Dict

from .runtime_env_default_registry import SITE_GROUP_ORDER, USER_GROUP_ORDER
from .runtime_env_default_registry import VERSION as REGISTRY_VERSION
from .runtime_env_defaults import VERSION as DEFAULTS_VERSION
from .runtime_env_defaults import apply_site_defaults, apply_user_defaults, env_bool
from .runtime_settings_ini_loader import VERSION as SETTINGS_INI_VERSION
from .runtime_settings_ini_loader import load_settings_ini

logger = logging.getLogger(__name__)
VERSION = "REV14-RUNTIME-ENV-DEFAULTS-PATCH-DATABASE-OWNER"
_INSTALLED = False


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


def _install_entry_fire_rescue(context: str) -> bool:
    try:
        if context not in {"main", "helper"}:
            return False
        if os.environ.get("DISABLE_ENTRY_FIRE_RESCUE_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] entry fire rescue disabled by env")
            return False
        from . import entry_fire_rescue_runtime_patch
        return bool(entry_fire_rescue_runtime_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] entry fire rescue install failed")
        return False


def _install_ranking_entry_runtime_rescue(context: str) -> bool:
    try:
        if context not in {"main", "helper"}:
            return False
        if os.environ.get("DISABLE_RANKING_ENTRY_RUNTIME_RESCUE_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] ranking entry runtime rescue disabled by env")
            return False
        from . import ranking_entry_runtime_rescue_patch
        return bool(ranking_entry_runtime_rescue_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] ranking entry runtime rescue install failed")
        return False


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
    try:
        if context not in {"main_database", "helper", "main"}:
            return False
        if os.environ.get("DISABLE_PUSH_REGISTER_RECOVERY_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] push register recovery disabled by env")
            return False
        from . import push_registration_recovery_patch
        return bool(push_registration_recovery_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] push register recovery install failed")
        return False


def _install_common_day_position_guard(context: str) -> bool:
    try:
        if context not in {"main", "helper"}:
            return False
        if os.environ.get("DISABLE_COMMON_ENTRY_DAY_POSITION_GUARD", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] common day position guard disabled by env")
            return False
        from . import common_entry_day_position_guard_patch
        return bool(common_entry_day_position_guard_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] common day position guard install failed")
        return False


def _install_strict_final_liquidity_guard(context: str) -> bool:
    try:
        if context not in {"main", "helper"}:
            return False
        if os.environ.get("DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] strict final liquidity guard disabled by env")
            return False
        from . import entry_handler_strict_recent_liquidity_patch
        return bool(entry_handler_strict_recent_liquidity_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] strict final liquidity guard install failed")
        return False


def _install_tonosama_exit_source_infer(context: str) -> bool:
    try:
        if context not in {"main", "helper"}:
            return False
        if os.environ.get("DISABLE_TONOSAMA_EXIT_SOURCE_INFER_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] tonosama exit infer disabled by env")
            return False
        from . import tonosama_exit_source_infer_patch
        return bool(tonosama_exit_source_infer_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] tonosama exit infer install failed")
        return False


def _install_tonosama_pending_candidate_audit(context: str) -> bool:
    try:
        if context not in {"main", "helper"}:
            return False
        if os.environ.get("DISABLE_TONOSAMA_PENDING_CANDIDATE_AUDIT_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] tonosama pending candidate audit disabled by env")
            return False
        from . import tonosama_pending_candidate_audit_patch
        return bool(tonosama_pending_candidate_audit_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] tonosama pending candidate audit install failed")
        return False


def _install_database_owner(context: str) -> bool:
    try:
        if context not in {"main", "helper", "main_database"}:
            return False
        if os.environ.get("DISABLE_DATABASE_OWNER_RUNTIME_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] database owner patch disabled by env")
            return False
        from . import database_owner_runtime_patch
        return bool(database_owner_runtime_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] database owner install failed")
        return False


def _install_entry_count_unblock(context: str) -> bool:
    try:
        if context not in {"main", "helper", "main_database"}:
            return False
        if os.environ.get("DISABLE_ENTRY_COUNT_UNBLOCK_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] entry count unblock disabled by env")
            return False
        from . import entry_count_unblock_runtime_patch
        return bool(entry_count_unblock_runtime_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] entry count unblock install failed")
        return False


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
        database_owner_ok = _install_database_owner(context)
        entry_count_unblock_ok = _install_entry_count_unblock(context)
        entry_fire_rescue_ok = _install_entry_fire_rescue(context)
        ranking_entry_rescue_ok = _install_ranking_entry_runtime_rescue(context)
        low_vol_guard_ok = _install_low_volatility_entry_guard(context)
        push_register_recovery_ok = _install_push_registration_recovery(context)
        day_position_guard_ok = _install_common_day_position_guard(context)
        strict_final_liq_ok = _install_strict_final_liquidity_guard(context)
        tonosama_exit_infer_ok = _install_tonosama_exit_source_infer(context)
        tonosama_pending_audit_ok = _install_tonosama_pending_candidate_audit(context)
        _INSTALLED = True
        logger.warning(
            "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s settings_ini=%s settings_applied=%s builtins_applied=%s context=%s site_groups=%s user_groups=%s database_owner=%s rescue=%s ranking_rescue=%s tonosama_rescue=%s entry_count_unblock=%s entry_fire_rescue=%s ranking_entry_rescue=%s low_vol_guard=%s push_register_recovery=%s day_position_guard=%s strict_final_liq=%s tonosama_exit_infer=%s tonosama_pending_audit=%s verbose=%s",
            VERSION,
            DEFAULTS_VERSION,
            REGISTRY_VERSION,
            SETTINGS_INI_VERSION,
            len(settings_applied),
            len(applied),
            context,
            ",".join(SITE_GROUP_ORDER),
            ",".join(USER_GROUP_ORDER),
            database_owner_ok,
            os.environ.get("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES"),
            os.environ.get("USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES"),
            os.environ.get("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES"),
            entry_count_unblock_ok,
            entry_fire_rescue_ok,
            ranking_entry_rescue_ok,
            low_vol_guard_ok,
            push_register_recovery_ok,
            day_position_guard_ok,
            strict_final_liq_ok,
            tonosama_exit_infer_ok,
            tonosama_pending_audit_ok,
            env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False),
        )
        return True
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] install failed")
        return False


__all__ = ["VERSION", "install"]
