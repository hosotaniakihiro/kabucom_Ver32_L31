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

VERSION = "REV2-RUNTIME-ENV-DEFAULTS-CENTRALIZED-SITE"

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


# PUSH core-integrated defaults. These reflect the mainline PUSH rotation
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

# Rescue/fail-open defaults. Safety-first: normal operation uses mainline
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
    "RANKING_FINAL_RESCUE_AI_FAILOPEN": "0",
    "ENTRY_DIRECTION_RECURSION_FAILOPEN_ENABLED": "0",
    "TONOSAMA_FAILOPEN_DIRECTION_RESCUE": "0",
    "TONOSAMA_ATR1M_FILTER_RESCUE": "0",
    "TONOSAMA_RANGE5M_FILTER_RESCUE": "0",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING": "0",
    "TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE": "0",
    "TONOSAMA_DROP_HISTORY_MISSING_ENTRY": "1",
    "LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK": "0",
}

# SQLite / DB-friendly defaults. These are intentionally small because DB
# processes should not receive trading decision patches.
DB_DEFAULTS: Dict[str, str] = {
    "SQLITE_MEMORY_PRAGMAS_ENABLED": "1",
    "SQLITE_MEMORY_TEMP_STORE": "MEMORY",
    "SQLITE_MEMORY_CACHE_KB": "-65536",
    "SQLITE_BUSY_TIMEOUT_MS": "5000",
    "SQLITE_MMAP_SIZE_BYTES": "268435456",
    "SQLITE_CACHE_SPILL_OFF": "1",
    "SQLITE_WAL_AUTOCHECKPOINT": "1000",
    "SQLITE_SYNCHRONOUS": "NORMAL",
}

# Non-main helpers should stay light unless explicitly promoted to full mode.
HELPER_DEFAULTS: Dict[str, str] = {
    "SITECUSTOMIZE_ENABLE_FULL_NONMAIN": "0",
}

# Ranking / entry safety defaults: keep safety guards on, but not fail-open.
RANKING_ENTRY_DEFAULTS: Dict[str, str] = {
    "RANKING_ENTRY_WATCHDOG_ENABLED": "1",
    "RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC": "55",
    "RANKING_ENTRY_HARD_TIMEOUT_ENABLED": "1",
    "RANKING_ENTRY_HARD_TIMEOUT_SEC": "28",
    "RANKING_ENTRY_SNAPSHOT_TECH_ALIAS_ENABLED": "1",
    "RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED": "1",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_PATCH_ENABLED": "1",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_LOOKBACK_ROWS": "12",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_MAX_AGE_MIN": "30",
    "RANKING_STUCK_PENDING_MAX_CONTROLLER_RETRY": "2",
    "RANKING_STUCK_PENDING_MAX_AGE_SEC": "120",
    "RANKING_FINAL_RESCUE_MIN_SCORE": "50",
    "RANKING_FINAL_RESCUE_ATR_MIN_RATIO": "0.0005",
    "LOW_MOVE_RANKING_MIN_ENTRY_PRICE": "300",
    "LOW_MOVE_RANKING_MAX_ENTRY_PRICE": "7000",
    "LOW_MOVE_RANKING_MIN_RANGE_PCT_LOW_PRICE": "0.008",
    "LOW_MOVE_RANKING_MIN_RANGE_PCT_HIGH_PRICE": "0.006",
    "LOW_MOVE_RANKING_STRONG_RANGE_PCT": "0.014",
    "LOW_MOVE_RANKING_MIN_ABS_SLOPE": "0.0000",
    "USERCUSTOMIZE_ALLOW_DUPLICATE_PATCHES": "0",
}

TONOSAMA_DEFAULTS: Dict[str, str] = {
    "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE": "3.0",
    "TONOSAMA_5SEC_ADVISORY_ENABLED": "1",
    "TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS": "0",
    "TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC": "1",
    "TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT": "0.0",
    "TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX": "1",
    "TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT": "0.50",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_ENABLED": "0",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_RANGE_PCT": "3.0",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_VOLUME": "50000",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE": "3.0",
    "TONOSAMA_ENTRY_TIMEOUT_SEC": "45",
    "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC": "12",
    "TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING": "1",
    "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC": "10",
    "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC": "60",
    "ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE": "1",
    "ENTRY_CONTROLLER_TONOSAMA_MIN_SCORE": "0.01",
    "LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE": "300",
    "FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK": "1",
    "FINAL_ENTRY_TONOSAMA_MIN_VOLUME": "30000",
    "FINAL_ENTRY_TONOSAMA_MIN_TURNOVER": "10000000",
}

ENTRY_DEFAULTS: Dict[str, str] = {
    "ENTRY_CONTROLLER_LOCK_WAIT_ENABLED": "1",
    "ENTRY_CONTROLLER_LOCK_WAIT_SOURCES": "RANKING,TONOSAMA,SUMMARY",
    "ENTRY_CONTROLLER_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED": "1",
    "ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL": "1",
    "ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED": "1",
    "ENTRY_SHORT_MTF_REQUIRED": "1",
    "ENTRY_SHORT_MTF_FORCE_2OF3": "1",
    "ENTRY_SHORT_MTF_MIN_ALIGNED": "2",
    "ENTRY_SHORT_MTF_MIN_AVAILABLE": "2",
    "ENTRY_SHORT_MTF_SLOPE_EPS": "0.0",
    "ENTRY_DAILY_MTF_OPTIONAL": "1",
    "ENTRY_SHORT_MTF_DB_BACKFILL": "1",
    "ENTRY_SHORT_MTF_ZERO_NEUTRAL": "1",
}

SUMMARY_YAHOO_DEFAULTS: Dict[str, str] = {
    "YAHOO_COMPLEMENT_DB_WARMUP_ENABLED": "1",
    "YAHOO_COMPLEMENT_DB_WARMUP_MIN_BARS": "75",
    "YAHOO_COMPLEMENT_DB_WARMUP_LOOKBACK_DAYS": "7",
    "SUMMARY_DB_DATE_GUARD_ENABLED": "1",
    "SUMMARY_DB_DATE_GUARD_CLEANUP_ENABLED": "0",
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


def apply_tonosama_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(TONOSAMA_DEFAULTS, environ=environ)


def apply_entry_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(ENTRY_DEFAULTS, environ=environ)


def apply_summary_yahoo_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(SUMMARY_YAHOO_DEFAULTS, environ=environ)


def apply_site_defaults(context: str = "unknown", *, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Defaults intended for sitecustomize.py."""
    applied: Dict[str, str] = {}
    for group in (
        apply_push_defaults,
        apply_rescue_defaults,
        apply_db_defaults,
        apply_helper_defaults,
        apply_ranking_entry_defaults,
        apply_tonosama_defaults,
        apply_entry_defaults,
        apply_summary_yahoo_defaults,
    ):
        applied.update(group(environ=environ))
    if env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False):
        print(f"[RUNTIME ENV DEFAULTS] site context={context} version={VERSION} applied={len(applied)}")
    return applied


def apply_user_defaults(context: str = "unknown", *, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Defaults intended for usercustomize.py."""
    applied: Dict[str, str] = {}
    for group in (
        apply_push_defaults,
        apply_rescue_defaults,
        apply_ranking_entry_defaults,
        apply_tonosama_defaults,
        apply_entry_defaults,
    ):
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
    "apply_tonosama_defaults",
    "apply_entry_defaults",
    "apply_summary_yahoo_defaults",
    "apply_site_defaults",
    "apply_user_defaults",
]
