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

VERSION = "REV2-RUNTIME-ENV-DEFAULT-REGISTRY-TRADE-CATEGORIES"


@dataclass(frozen=True)
class RuntimeDefaultGroup:
    """Metadata for one runtime default group."""

    name: str
    dict_name: str
    owner: str
    purpose: str
    settings_section: str
    safety: str


@dataclass(frozen=True)
class RuntimeDefaultKeyCategory:
    """Metadata for a key-level settings.ini migration category."""

    name: str
    settings_section: str
    purpose: str
    env_keys: Tuple[str, ...]


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

# Key-level categories do not change runtime behavior.  They document the
# first settings.ini split targets inside the larger default dictionaries.
KEY_CATEGORIES: Tuple[RuntimeDefaultKeyCategory, ...] = (
    RuntimeDefaultKeyCategory(
        name="trade_budget",
        settings_section="trade_budget",
        purpose="Capital allocation, candidate count, and lot sizing knobs that should eventually live in settings.ini.",
        env_keys=(
            "ENTRY_RANKING_SCALP_MAX_PRICE",
            "ENTRY_RANKING_SCALP_MIN_PRICE",
            "ENTRY_RANKING_SCALP_MIN_MTF",
            "PULLBACK_ENTRY_LOT_RATIO",
            "PULLBACK_ENTRY_MAX_CANDIDATES",
        ),
    ),
    RuntimeDefaultKeyCategory(
        name="order",
        settings_section="order",
        purpose="Broker routing and order exchange defaults.  Exchange=9 is order/SOR routing, not PUSH registration exchange.",
        env_keys=(
            "ENTRY_ORDER_EXCHANGE",
            "KABU_ORDER_EXCHANGE",
        ),
    ),
    RuntimeDefaultKeyCategory(
        name="entry_limits",
        settings_section="entry_limits",
        purpose="Final entry liquidity, board-missing allowance, watchlist liquidity, and pullback-entry thresholds.",
        env_keys=(
            "WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME",
            "WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME",
            "WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN",
            "ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME",
            "ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER",
            "ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE",
            "ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE",
            "ENTRY_BOARD_MISSING_QTY_RATIO",
            "PULLBACK_ENTRY_ENABLED",
            "PULLBACK_ENTRY_MIN_PULLBACK_PCT",
            "PULLBACK_ENTRY_MAX_PULLBACK_PCT",
            "PULLBACK_ENTRY_NEAR_MA_PCT",
            "PULLBACK_ENTRY_MIN_REBOUND_VOL_RATIO",
            "PULLBACK_ENTRY_MIN_VOLUME",
            "PULLBACK_ENTRY_MIN_TURNOVER",
        ),
    ),
    RuntimeDefaultKeyCategory(
        name="daily_risk",
        settings_section="daily_risk",
        purpose="Daily and symbol stop toggles.  Current policy keeps all-loss/first-loss stop guards disabled by default.",
        env_keys=(
            "SUMMARY_AI_PRE_FILTER_DAILY_RISK",
            "SUMMARY_AI_DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS",
            "DAILY_RISK_SYMBOL_STOP_AFTER_FIRST_LOSS",
            "DAILY_RISK_STOP_AFTER_FIRST_LOSS",
            "SYMBOL_STOP_AFTER_FIRST_LOSS_ENABLED",
        ),
    ),
)

KEY_CATEGORY_BY_NAME: Dict[str, RuntimeDefaultKeyCategory] = {
    category.name: category for category in KEY_CATEGORIES
}
ENV_KEY_TO_CATEGORY: Dict[str, RuntimeDefaultKeyCategory] = {
    key: category for category in KEY_CATEGORIES for key in category.env_keys
}

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


def iter_key_categories() -> Iterable[RuntimeDefaultKeyCategory]:
    """Return key-level settings.ini migration categories."""
    return iter(KEY_CATEGORIES)


def group_names(order: Tuple[str, ...]) -> Tuple[str, ...]:
    """Validate and return a group-order tuple."""
    for name in order:
        if name not in GROUP_BY_NAME:
            raise KeyError(f"unknown runtime default group: {name}")
    return order


__all__ = [
    "VERSION",
    "RuntimeDefaultGroup",
    "RuntimeDefaultKeyCategory",
    "GROUPS",
    "GROUP_BY_NAME",
    "DICT_TO_GROUP",
    "KEY_CATEGORIES",
    "KEY_CATEGORY_BY_NAME",
    "ENV_KEY_TO_CATEGORY",
    "SITE_GROUP_ORDER",
    "USER_GROUP_ORDER",
    "DB_MINIMAL_GROUP_ORDER",
    "HELPER_MINIMAL_GROUP_ORDER",
    "iter_groups",
    "iter_key_categories",
    "group_names",
]
