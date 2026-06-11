# -*- coding: utf-8 -*-
"""
Centralized runtime environment defaults for startup customization.

This module is intentionally conservative:
- It only fills missing environment variables by default.
- Existing operator/task-scheduler settings always win.
- sitecustomize.py / usercustomize.py can import this module safely.

The goal is to gradually move scattered os.environ.setdefault(...) calls
from sitecustomize.py, usercustomize.py, and compatibility patches into one
small, auditable place.
"""
from __future__ import annotations

import os
from typing import Dict, Mapping, MutableMapping, Optional

VERSION = "REV1-RUNTIME-ENV-DEFAULTS"

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def env_bool(name: str, default: bool = False) -> bool:
    """Return a tolerant boolean value from os.environ."""
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    val = str(raw).strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return bool(default)


def _set_defaults(defaults: Mapping[str, str], *, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Set missing environment variables and return keys that were filled."""
    env = environ if environ is not None else os.environ
    applied: Dict[str, str] = {}
    for key, value in defaults.items():
        if key not in env:
            env[key] = str(value)
            applied[key] = str(value)
    return applied


# PUSH core-integrated defaults.  These reflect the mainline PUSH rotation
# implementation and keep old monkey-patch shims off by default.
PUSH_DEFAULTS: Dict[str, str] = {
    "PUSH_WS_VENDOR_RUN_FOREVER": "1",
    "PUSH_WS_ENABLE_PING": "0",
    "PUSH_ROTATION_CLOSE_BEFORE_REGISTER": "1",
    "PUSH_ROTATION_WAIT_WS_READY_AFTER_REGISTER": "1",
    "PUSH_ROTATION_HOLD_SEC": "4.8",
    "PUSH_ROTATION_CLEAR_GAP_SEC": "0.2",
    "USERCUSTOMIZE_ENABLE_LEGACY_PUSH_PATCHES": "0",
}

# Rescue/fail-open defaults.  Safety-first: normal operation uses mainline
# checks; rescue layers are opt-in via explicit environment variables.
RESCUE_DEFAULTS: Dict[str, str] = {
    "SITECUSTOMIZE_ENABLE_RESCUE_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_ENTRY_FAILOPEN_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_RANKING_FINAL_RESCUE_PATCH": "0",
    "SITECUSTOMIZE_ENABLE_TONOSAMA_EXTRA_RESCUE_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_SUMMARY_AI_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_LEGACY_RANKING_FAILOPEN_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_LEGACY_TONOSAMA_FAILOPEN_PATCHES": "0",
    "RANKING_AI_GATE_FAILOPEN_ENABLED": "0",
    "TONOSAMA_FAILOPEN_DIRECTION_RESCUE": "0",
    "TONOSAMA_ATR1M_FILTER_RESCUE": "0",
    "TONOSAMA_RANGE5M_FILTER_RESCUE": "0",
}

# SQLite / DB-friendly defaults.  These are intentionally small because DB
# processes should not receive trading decision patches.
DB_DEFAULTS: Dict[str, str] = {
    "SQLITE_BUSY_TIMEOUT_MS": "5000",
    "SQLITE_WAL_AUTOCHECKPOINT": "1000",
    "SQLITE_SYNCHRONOUS": "NORMAL",
}

# Non-main helpers should stay light unless explicitly promoted to full mode.
HELPER_DEFAULTS: Dict[str, str] = {
    "SITECUSTOMIZE_ENABLE_FULL_NONMAIN": "0",
}

# Ranking / entry safety defaults: keep safety guards on, but not fail-open.
RANKING_ENTRY_DEFAULTS: Dict[str, str] = {
    "USERCUSTOMIZE_ALLOW_DUPLICATE_PATCHES": "0",
}


def apply_push_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(PUSH_DEFAULTS, environ=environ)


def apply_rescue_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(RESCUE_DEFAULTS, environ=environ)


def apply_db_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(DB_DEFAULTS, environ=environ)


def apply_helper_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(HELPER_DEFAULTS, environ=environ)


def apply_ranking_entry_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(RANKING_ENTRY_DEFAULTS, environ=environ)


def apply_site_defaults(context: str = "unknown", *, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Defaults intended for sitecustomize.py."""
    applied: Dict[str, str] = {}
    for group in (apply_push_defaults, apply_rescue_defaults, apply_db_defaults, apply_helper_defaults):
        applied.update(group(environ=environ))
    if env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False):
        print(f"[RUNTIME ENV DEFAULTS] site context={context} version={VERSION} applied={len(applied)}")
    return applied


def apply_user_defaults(context: str = "unknown", *, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Defaults intended for usercustomize.py."""
    applied: Dict[str, str] = {}
    for group in (apply_push_defaults, apply_rescue_defaults, apply_ranking_entry_defaults):
        applied.update(group(environ=environ))
    if env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False):
        print(f"[RUNTIME ENV DEFAULTS] user context={context} version={VERSION} applied={len(applied)}")
    return applied


__all__ = [
    "VERSION",
    "env_bool",
    "apply_push_defaults",
    "apply_rescue_defaults",
    "apply_db_defaults",
    "apply_helper_defaults",
    "apply_ranking_entry_defaults",
    "apply_site_defaults",
    "apply_user_defaults",
]
