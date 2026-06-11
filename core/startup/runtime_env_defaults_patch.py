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

logger = logging.getLogger(__name__)
VERSION = "REV2-RUNTIME-ENV-DEFAULTS-PATCH-REGISTRY-AWARE"
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


def install() -> bool:
    """Apply centralized defaults once.

    Returns True when defaults are available. Existing environment variables are
    not overwritten by apply_*_defaults(); operator overrides remain intact.
    """
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        context = _argv_context()
        applied: Dict[str, str] = {}
        applied.update(apply_site_defaults(context=context))
        applied.update(apply_user_defaults(context=context))

        _INSTALLED = True
        if env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False):
            logger.warning(
                "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s applied=%s context=%s site_groups=%s user_groups=%s",
                VERSION,
                DEFAULTS_VERSION,
                REGISTRY_VERSION,
                len(applied),
                context,
                ",".join(SITE_GROUP_ORDER),
                ",".join(USER_GROUP_ORDER),
            )
        else:
            logger.warning(
                "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s applied=%s context=%s rescue=%s ranking_rescue=%s tonosama_rescue=%s",
                VERSION,
                DEFAULTS_VERSION,
                REGISTRY_VERSION,
                len(applied),
                context,
                os.environ.get("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES"),
                os.environ.get("USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES"),
                os.environ.get("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES"),
            )
        return True
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] install failed")
        return False


__all__ = ["VERSION", "install"]
