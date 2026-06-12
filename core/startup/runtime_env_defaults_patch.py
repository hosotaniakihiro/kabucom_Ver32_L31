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
VERSION = "REV5-RUNTIME-ENV-DEFAULTS-PATCH-RANKING-ENTRY-RESCUE"
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


def install() -> bool:
    """Apply centralized defaults once.

    Order of precedence:
    1. Explicit process environment variables win.
    2. Optional settings.ini fills missing values when present.
    3. Built-in centralized defaults fill the remaining missing values.
    4. Trading-process runtime rescue patches may add safe fail-open defaults.
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

        _INSTALLED = True
        if env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False):
            logger.warning(
                "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s settings_ini=%s settings_applied=%s builtins_applied=%s context=%s site_groups=%s user_groups=%s entry_fire_rescue=%s ranking_entry_rescue=%s",
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
            )
        else:
            logger.warning(
                "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s settings_ini=%s settings_applied=%s builtins_applied=%s context=%s rescue=%s ranking_rescue=%s tonosama_rescue=%s entry_fire_rescue=%s ranking_entry_rescue=%s",
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
            )
        return True
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] install failed")
        return False


__all__ = ["VERSION", "install"]
