# -*- coding: utf-8 -*-
"""
Optional settings.ini loader for runtime environment defaults.

This loader is intentionally conservative:
- It does nothing when settings.ini is missing.
- It only sets environment variables that are not already defined.
- It applies keys only from known runtime sections by default.
- Unknown/legacy sections are reported but ignored unless explicitly enabled.
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

VERSION = "REV2-OPTIONAL-SETTINGS-INI-LOADER-KNOWN-SECTIONS-ONLY"

_TRUE_VALUES = {"1", "true", "TRUE", "yes", "YES", "on", "ON"}

# INI key aliases.  Keep keys lowercase because configparser lower-cases option
# names by default in many examples.  parser.optionxform preserves case, so we
# lower-case keys inside _env_name.
_KEY_ALIASES = {
    "entry_order_exchange": "ENTRY_ORDER_EXCHANGE",
    "kabu_order_exchange": "KABU_ORDER_EXCHANGE",
}

# Only these sections are runtime-env sections.  Existing legacy sections such as
# [aukabu], [WebSocket], [discord], [trade], [orders], etc. must not be promoted
# into os.environ automatically because they may contain unrelated app settings
# or credentials.
_KNOWN_SECTIONS = {
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
    for section in parser.sections():
        is_known = section in _KNOWN_SECTIONS
        if not is_known:
            unknown_sections.append(section)
            if not allow_unknown:
                ignored_unknown_items += len(list(parser.items(section)))
                continue
        for key, raw_value in parser.items(section):
            env_name = _env_name(section, key)
            value = str(raw_value).strip()
            if value == "":
                continue
            if env_name in os.environ:
                continue
            os.environ[env_name] = value
            applied[env_name] = value

    logger.warning(
        "[RUNTIME SETTINGS INI] loaded version=%s path=%s context=%s applied=%s unknown_sections=%s ignored_unknown_items=%s allow_unknown=%s",
        VERSION,
        chosen,
        context,
        len(applied),
        ",".join(unknown_sections) if unknown_sections else "-",
        ignored_unknown_items,
        int(allow_unknown),
    )
    return applied


__all__ = ["VERSION", "load_settings_ini", "_KNOWN_SECTIONS", "_env_name"]
