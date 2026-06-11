# -*- coding: utf-8 -*-
"""
Registry metadata for centralized runtime environment defaults.

This module does not set any environment variables.  It documents how
core.startup.runtime_env_defaults is grouped so future migration to
settings.ini can be done category-by-category instead of by searching
through startup files.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

VERSION = "REV1-RUNTIME-ENV-DEFAULT-REGISTRY"


@dataclass(frozen=True)
class RuntimeDefaultGroup:
    """Metadata for one runtime default group."""

    name: str
    dict_name: str
    owner: str
    purpose: str
    settings_section: str
    safety: str


GROUPS: Tuple[RuntimeDefaultGroup, ...] = (
    RuntimeDefaultGroup(
        name="push",
        dict_name="PUSH_DEFAULTS",
        owner="trading.push",
        purpose="PUSH websocket, A/B rotation, owner-lock, and core-integrated PUSH behavior.",
        settings_section="push",
        safety="core-default; legacy shims disabled unless explicitly enabled",
    ),
    RuntimeDefaultGroup(
        name="rescue",
        dict_name="RESCUE_DEFAULTS",
        owner="startup",
        purpose="Global fail-open/rescue feature switches. Defaults should stay safety-first/off.",
        settings_section="rescue",
        safety="opt-in only",
    ),
    RuntimeDefaultGroup(
        name="db",
        dict_name="DB_DEFAULTS",
        owner="database",
        purpose="SQLite pragmas and DB-friendly runtime defaults for data collection processes.",
        settings_section="database",
        safety="safe for DB/main/helper processes",
    ),
    RuntimeDefaultGroup(
        name="helper",
        dict_name="HELPER_DEFAULTS",
        owner="startup",
        purpose="Non-main helper process loading mode switches.",
        settings_section="startup.helper",
        safety="minimal by default",
    ),
    RuntimeDefaultGroup(
        name="main_restore",
        dict_name="MAIN_RESTORE_DEFAULTS",
        owner="main.py",
        purpose="Restore main.py entry, exit, summary, ranking, and tonosama loops after earlier emergency disables.",
        settings_section="main",
        safety="enables production loops",
    ),
    RuntimeDefaultGroup(
        name="ranking_entry",
        dict_name="RANKING_ENTRY_DEFAULTS",
        owner="trading.ranking / trading.entry",
        purpose="Ranking entry watchdog, stale fail-closed, low-move filters, and ranking scalp thresholds.",
        settings_section="ranking_entry",
        safety="safety guards on; fail-open off",
    ),
    RuntimeDefaultGroup(
        name="tonosama",
        dict_name="TONOSAMA_DEFAULTS",
        owner="trading.tonosama / entry controller",
        purpose="Tonosama entry timeout, 5-second advisory, liquidity, stale MTF, and history guards.",
        settings_section="tonosama",
        safety="primary judgement on; rescue/fail-open off",
    ),
    RuntimeDefaultGroup(
        name="entry",
        dict_name="ENTRY_DEFAULTS",
        owner="trading.entry_exit / entry controller",
        purpose="Final entry controls, MTF requirements, exchange routing, risk stop toggles, liquidity guards, and pullback entry.",
        settings_section="entry",
        safety="final safety and routing defaults",
    ),
    RuntimeDefaultGroup(
        name="summary_yahoo",
        dict_name="SUMMARY_YAHOO_DEFAULTS",
        owner="trading.summary / trading.yahoo",
        purpose="Summary DB/Yahoo complement defaults and date guard behavior.",
        settings_section="summary_yahoo",
        safety="main.py skips heavy complement by default",
    ),
)

GROUP_BY_NAME: Dict[str, RuntimeDefaultGroup] = {group.name: group for group in GROUPS}
DICT_TO_GROUP: Dict[str, RuntimeDefaultGroup] = {group.dict_name: group for group in GROUPS}

# Applying order is intentionally explicit.  Keep this in sync with
# runtime_env_defaults.apply_site_defaults/apply_user_defaults.
SITE_GROUP_ORDER: Tuple[str, ...] = (
    "push",
    "rescue",
    "db",
    "helper",
    "ranking_entry",
    "tonosama",
    "entry",
    "summary_yahoo",
)

USER_GROUP_ORDER: Tuple[str, ...] = (
    "push",
    "rescue",
    "main_restore",
    "ranking_entry",
    "tonosama",
    "entry",
    "summary_yahoo",
)

DB_MINIMAL_GROUP_ORDER: Tuple[str, ...] = ("db",)
HELPER_MINIMAL_GROUP_ORDER: Tuple[str, ...] = ("db", "helper")


def iter_groups() -> Iterable[RuntimeDefaultGroup]:
    """Return default groups in registry order."""
    return iter(GROUPS)


def group_names(order: Tuple[str, ...]) -> Tuple[str, ...]:
    """Validate and return a group-order tuple."""
    for name in order:
        if name not in GROUP_BY_NAME:
            raise KeyError(f"unknown runtime default group: {name}")
    return order


__all__ = [
    "VERSION",
    "RuntimeDefaultGroup",
    "GROUPS",
    "GROUP_BY_NAME",
    "DICT_TO_GROUP",
    "SITE_GROUP_ORDER",
    "USER_GROUP_ORDER",
    "DB_MINIMAL_GROUP_ORDER",
    "HELPER_MINIMAL_GROUP_ORDER",
    "iter_groups",
    "group_names",
]
