# -*- coding: utf-8 -*-
"""
Optional settings.ini loader for runtime environment defaults.

This loader is intentionally conservative:
- It does nothing when settings.ini is missing.
- It only sets environment variables that are not already defined.
- It applies keys from runtime and existing legacy application sections.
- Unknown sections are reported but ignored unless explicitly enabled.
- It is called before centralized built-in defaults, so settings.ini can provide
  local defaults while explicit process environment variables still win.
"""
from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

VERSION = "REV3-SETTINGS-INI-LEGACY-SECTIONS-CASE-INSENSITIVE"

_TRUE_VALUES = {"1", "true", "TRUE", "yes", "YES", "on", "ON"}

# INI key aliases.  Keep keys lowercase because configparser lower-cases option
# names by default in many examples.  parser.optionxform preserves case, so we
# lower-case keys inside _env_name.
_KEY_ALIASES = {
    "entry_order_exchange": "ENTRY_ORDER_EXCHANGE",
    "kabu_order_exchange": "KABU_ORDER_EXCHANGE",
    "exchange": "ENTRY_ORDER_EXCHANGE",
    "orders_exchange": "ENTRY_ORDER_EXCHANGE",
    "order_exchange": "ENTRY_ORDER_EXCHANGE",
    "max_oneshot": "MAX_ONESHOT",
    "max_order_budget": "MAX_ONESHOT",
    "entry_budget": "MAX_ONESHOT",
    "max_pending": "ENTRY_MAX_PENDING",
    "max_pending_orders": "ENTRY_MAX_PENDING",
    "entry_max_pending": "ENTRY_MAX_PENDING",
    "max_positions": "ENTRY_MAX_PENDING",
    "entry_cutoff_time": "ENTRY_CUTOFF_TIME",
    "entry_no_new_after": "ENTRY_NO_NEW_AFTER",
    "no_new_entry_after": "ENTRY_NO_NEW_AFTER",
    "min_volume": "ENTRY_MIN_VOLUME",
    "min_turnover": "ENTRY_MIN_TURNOVER",
    "min_turnover_yen": "ENTRY_MIN_TURNOVER",
    "max_price": "ENTRY_MAX_PRICE",
    "min_price": "ENTRY_MIN_PRICE",
}

# Runtime sections used by the centralized defaults.
_RUNTIME_SECTIONS = {
    "trade_budget",
    "order",
    "entry_limits",
    "daily_risk",
    "push",
    "rescue",
    "database",
    "ranking_entry",
    "tonosama",
    "summary_yahoo",
}

# Existing settings.ini sections used by this project.  Older settings.ini files
# store most runtime values under these names, so treating only _RUNTIME_SECTIONS
# as known causes logs like: applied=0 unknown_sections=trade,orders,ENTRY ...
# and local settings never override built-in defaults.
_LEGACY_APP_SECTIONS = {
    "aukabu",
    "websocket",
    "discord",
    "gemini",
    "alerts",
    "paths",
    "strategy",
    "news",
    "trade",
    "orders",
    "trailing",
    "exit",
    "watchlist",
    "ranking",
    "entry",
    "entry_filter_1min",
    "entry_filter_3min",
    "entry_filter_5min",
    "test",
}

_KNOWN_SECTIONS = _RUNTIME_SECTIONS | _LEGACY_APP_SECTIONS

# Some legacy sections contain display-only or explanatory fields.  They are not
# harmful, but exporting very generic names such as NAME/TITLE can accidentally
# shadow unrelated process variables.  Keep the skip list intentionally tiny.
_SKIP_ENV_NAMES = {
    "NAME",
    "TITLE",
    "DESCRIPTION",
    "COMMENT",
    "MEMO",
}


def _repo_root() -> Path:
    # core/startup/runtime_settings_ini_loader.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2]


def _env_on(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) in _TRUE_VALUES


def _candidate_paths(explicit_path: Optional[str] = None) -> Iterable[Path]:
    if explicit_path:
        yield Path(explicit_path).expanduser()
        return

    env_path = os.environ.get("RUNTIME_SETTINGS_INI")
    if env_path:
        yield Path(env_path).expanduser()
        return

    root = _repo_root()
    yield root / "settings.ini"


def _env_name(section: str, key: str) -> str:
    key_l = key.strip().lower()
    if key_l in _KEY_ALIASES:
        return _KEY_ALIASES[key_l]
    return key_l.upper()


def load_settings_ini(*, context: str = "unknown", path: Optional[str] = None) -> Dict[str, str]:
    """Load optional settings.ini values into os.environ using setdefault.

    Returns a dictionary of env names that were applied.
    """
    applied: Dict[str, str] = {}
    chosen: Optional[Path] = None
    for candidate in _candidate_paths(path):
        try:
            if candidate.exists() and candidate.is_file():
                chosen = candidate
                break
        except OSError:
            logger.warning("[RUNTIME SETTINGS INI] cannot inspect path=%s", candidate, exc_info=True)

    if chosen is None:
        if _env_on("RUNTIME_SETTINGS_INI_VERBOSE"):
            logger.warning("[RUNTIME SETTINGS INI] missing; skipped context=%s", context)
        return applied

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # preserve case for values/logs; aliases still lower-case internally.
    try:
        parser.read(chosen, encoding="utf-8")
    except Exception:
        logger.exception("[RUNTIME SETTINGS INI] read failed path=%s context=%s", chosen, context)
        return applied

    allow_unknown = _env_on("RUNTIME_SETTINGS_INI_ALLOW_UNKNOWN_SECTIONS")
    unknown_sections = []
    ignored_unknown_items = 0
    skipped_existing = 0
    skipped_empty = 0
    skipped_generic = 0

    for section in parser.sections():
        section_norm = section.strip().lower()
        is_known = section_norm in _KNOWN_SECTIONS
        if not is_known:
            unknown_sections.append(section)
            if not allow_unknown:
                ignored_unknown_items += len(list(parser.items(section)))
                continue

        for key, raw_value in parser.items(section):
            env_name = _env_name(section, key)
            if env_name in _SKIP_ENV_NAMES:
                skipped_generic += 1
                continue
            value = str(raw_value).strip()
            if value == "":
                skipped_empty += 1
                continue
            if env_name in os.environ:
                skipped_existing += 1
                continue
            os.environ[env_name] = value
            applied[env_name] = value

    logger.warning(
        "[RUNTIME SETTINGS INI] loaded version=%s path=%s context=%s applied=%s unknown_sections=%s ignored_unknown_items=%s skipped_existing=%s skipped_empty=%s skipped_generic=%s allow_unknown=%s known_sections=%s",
        VERSION,
        chosen,
        context,
        len(applied),
        ",".join(unknown_sections) if unknown_sections else "-",
        ignored_unknown_items,
        skipped_existing,
        skipped_empty,
        skipped_generic,
        int(allow_unknown),
        len(_KNOWN_SECTIONS),
    )
    return applied


__all__ = ["VERSION", "load_settings_ini", "_KNOWN_SECTIONS", "_env_name"]
