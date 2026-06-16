# -*- coding: utf-8 -*-
"""
Compatibility installer for centralized runtime environment defaults.

This is a small bridge module so startup loaders can use the same install()
pattern as the existing core.startup.*_patch modules.
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
VERSION = "REV12-RUNTIME-ENV-DEFAULTS-PATCH-TONOSAMA-CANDIDATE-AUDIT"
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
    """Install the entry firing rescue patch in trading processes only."""
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
    """Install narrow RANKING entry rescue for mtf_low and zero-range startup rows."""
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
    """Install final low-volatility veto for trading entry processes."""
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
    """Install PUSH register 4001009 retry and target top-up in DB/PUSH processes."""
    try:
        # main_database.py and helper runners include push_receiver_runner.py.
        # Installing in main is also safe because the patch only wraps modules if imported.
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
    """Install common intraday position veto for TONOSAMA/RANKING/SUMMARY pending entries."""
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
    """Install fail-closed final liquidity guard at the order-dispatch layer."""
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
    """Ensure TONOSAMA/殿様イナゴ entries use the dedicated fast exit path."""
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
    """Record accepted TONOSAMA pending entries into audit.candidate_history."""
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


def install() -> bool:
    """Apply centralized defaults once.

    Order of precedence:
    1. Explicit process environment variables win.
    2. Optional settings.ini fills missing values when present.
    3. Built-in centralized defaults fill the remaining missing values.
    4. Trading-process runtime rescue patches may add safe fail-open defaults.
    5. Final risk veto patches such as low-volatility guard remain fail-closed.
    6. PUSH register recovery keeps main_database.py registration healthy.
    7. Common day-position guard blocks late SELL/BUY extremes across entry sources.
    8. Strict final liquidity guard blocks thin/stale symbols immediately before order send.
    9. TONOSAMA exit inference routes 殿様イナゴ positions to fast scalping exit.
    10. TONOSAMA pending candidate audit persists detailed inago entry conditions.
    """
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        context = _argv_context()
        settings_applied: Dict[str, str] = load_settings_ini(context=context)
        applied: Dict[str, str] = {}
        applied.update(apply_site_defaults(context=context))
        applied.update(apply_user_defaults(context=context))
        entry_fire_rescue_ok = _install_entry_fire_rescue(context)
        ranking_entry_rescue_ok = _install_ranking_entry_runtime_rescue(context)
        low_vol_guard_ok = _install_low_volatility_entry_guard(context)
        push_register_recovery_ok = _install_push_registration_recovery(context)
        day_position_guard_ok = _install_common_day_position_guard(context)
        strict_final_liq_ok = _install_strict_final_liquidity_guard(context)
        tonosama_exit_infer_ok = _install_tonosama_exit_source_infer(context)
        tonosama_pending_audit_ok = _install_tonosama_pending_candidate_audit(context)

        _INSTALLED = True
        if env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False):
            logger.warning(
                "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s settings_ini=%s settings_applied=%s builtins_applied=%s context=%s site_groups=%s user_groups=%s entry_fire_rescue=%s ranking_entry_rescue=%s low_vol_guard=%s push_register_recovery=%s day_position_guard=%s strict_final_liq=%s tonosama_exit_infer=%s tonosama_pending_audit=%s",
                VERSION,
                DEFAULTS_VERSION,
                REGISTRY_VERSION,
                SETTINGS_INI_VERSION,
                len(settings_applied),
                len(applied),
                context,
                ",".join(SITE_GROUP_ORDER),
                ",".join(USER_GROUP_ORDER),
                entry_fire_rescue_ok,
                ranking_entry_rescue_ok,
                low_vol_guard_ok,
                push_register_recovery_ok,
                day_position_guard_ok,
                strict_final_liq_ok,
                tonosama_exit_infer_ok,
                tonosama_pending_audit_ok,
            )
        else:
            logger.warning(
                "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s settings_ini=%s settings_applied=%s builtins_applied=%s context=%s rescue=%s ranking_rescue=%s tonosama_rescue=%s entry_fire_rescue=%s ranking_entry_rescue=%s low_vol_guard=%s push_register_recovery=%s day_position_guard=%s strict_final_liq=%s tonosama_exit_infer=%s tonosama_pending_audit=%s",
                VERSION,
                DEFAULTS_VERSION,
                REGISTRY_VERSION,
                SETTINGS_INI_VERSION,
                len(settings_applied),
                len(applied),
                context,
                os.environ.get("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES"),
                os.environ.get("USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES"),
                os.environ.get("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES"),
                entry_fire_rescue_ok,
                ranking_entry_rescue_ok,
                low_vol_guard_ok,
                push_register_recovery_ok,
                day_position_guard_ok,
                strict_final_liq_ok,
                tonosama_exit_infer_ok,
                tonosama_pending_audit_ok,
            )
        return True
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] install failed")
        return False


__all__ = ["VERSION", "install"]
