# -*- coding: utf-8 -*-
"""
Validate optional settings.ini without starting the trading system.

Usage:
    python tools/validate_settings_ini.py
    python tools/validate_settings_ini.py --path F:\\script\\python\\kabu\\kabucom_Ver32_L31\\settings.ini
    python tools/validate_settings_ini.py --show-values

This script is intentionally read-only:
- It does not import trading loops.
- It does not call kabu Station APIs.
- It does not open SQLite databases.
- It does not place orders.
"""
from __future__ import annotations

import argparse
import configparser
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.startup.runtime_env_default_registry import (  # noqa: E402
    ENV_KEY_TO_CATEGORY,
    GROUP_BY_NAME,
    KEY_CATEGORY_BY_NAME,
    SITE_GROUP_ORDER,
    USER_GROUP_ORDER,
    VERSION as REGISTRY_VERSION,
)
from core.startup.runtime_settings_ini_loader import (  # noqa: E402
    VERSION as LOADER_VERSION,
    _KNOWN_SECTIONS,  # diagnostic script; ok to inspect loader metadata
    _env_name,
)

VERSION = "REV1-SETTINGS-INI-VALIDATOR"


def _default_path() -> Path:
    return REPO_ROOT / "settings.ini"


def _example_path() -> Path:
    return REPO_ROOT / "settings.ini.example"


def _read_ini(path: Path) -> Tuple[configparser.ConfigParser, List[str]]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    errors: List[str] = []
    try:
        read_files = parser.read(path, encoding="utf-8")
        if not read_files:
            errors.append(f"cannot read ini: {path}")
    except configparser.Error as exc:
        errors.append(f"ini parse error: {exc}")
    except OSError as exc:
        errors.append(f"ini open error: {exc}")
    return parser, errors


def _section_status(sections: Iterable[str]) -> Tuple[List[str], List[str]]:
    known = set(_KNOWN_SECTIONS)
    existing = set(sections)
    unknown = sorted(existing - known)
    missing = sorted(known - existing)
    return unknown, missing


def _collect_envs(parser: configparser.ConfigParser) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    env_values: Dict[str, str] = {}
    duplicate_sources: Dict[str, List[str]] = {}
    for section in parser.sections():
        for key, raw_value in parser.items(section):
            env_name = _env_name(section, key)
            source = f"[{section}] {key}"
            value = str(raw_value).strip()
            if value == "":
                continue
            if env_name in env_values:
                duplicate_sources.setdefault(env_name, []).append(source)
            else:
                env_values[env_name] = value
                duplicate_sources.setdefault(env_name, [source])
    duplicates = {k: v for k, v in duplicate_sources.items() if len(v) > 1}
    return env_values, duplicates


def _category_name(env_name: str) -> str:
    category = ENV_KEY_TO_CATEGORY.get(env_name)
    if category is not None:
        return category.name
    for name, group in GROUP_BY_NAME.items():
        # Group-level categories are not key-complete yet.  Use broad fallback by prefixes.
        if group.settings_section in ("push", "database", "ranking_entry", "tonosama", "entry", "summary_yahoo"):
            section = group.settings_section.upper()
            if env_name.startswith(section) or env_name.startswith(name.upper()):
                return name
    return "uncategorized"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate optional settings.ini for runtime env defaults.")
    parser.add_argument("--path", default=None, help="settings.ini path. Default: repository settings.ini")
    parser.add_argument("--show-values", action="store_true", help="Print non-empty values. Use carefully for local secrets.")
    parser.add_argument("--use-example", action="store_true", help="Validate settings.ini.example instead of settings.ini.")
    args = parser.parse_args(argv)

    path = _example_path() if args.use_example else Path(args.path).expanduser() if args.path else _default_path()

    print(f"[settings validator] version={VERSION}")
    print(f"[settings validator] registry={REGISTRY_VERSION} loader={LOADER_VERSION}")
    print(f"[settings validator] path={path}")
    print(f"[settings validator] site_group_order={','.join(SITE_GROUP_ORDER)}")
    print(f"[settings validator] user_group_order={','.join(USER_GROUP_ORDER)}")

    if not path.exists():
        print("[settings validator] status=missing")
        print("[settings validator] hint=copy settings.ini.example to settings.ini when you want local overrides")
        return 0

    cfg, errors = _read_ini(path)
    if errors:
        for err in errors:
            print(f"[settings validator] ERROR {err}")
        return 2

    unknown_sections, missing_sections = _section_status(cfg.sections())
    env_values, duplicates = _collect_envs(cfg)

    print(f"[settings validator] status=ok sections={len(cfg.sections())} env_values={len(env_values)}")
    if unknown_sections:
        print(f"[settings validator] unknown_sections={','.join(unknown_sections)}")
    else:
        print("[settings validator] unknown_sections=-")
    if missing_sections:
        print(f"[settings validator] missing_known_sections={','.join(missing_sections)}")
    else:
        print("[settings validator] missing_known_sections=-")

    if duplicates:
        print("[settings validator] duplicate_env_names detected")
        for env_name, sources in sorted(duplicates.items()):
            print(f"  {env_name}: {'; '.join(sources)}")
        return 3

    category_counts: Dict[str, int] = {}
    for env_name in env_values:
        category_counts[_category_name(env_name)] = category_counts.get(_category_name(env_name), 0) + 1
    for category, count in sorted(category_counts.items()):
        print(f"[settings validator] category={category} count={count}")

    explicit_env_wins = sorted(k for k in env_values if k in os.environ)
    if explicit_env_wins:
        print(f"[settings validator] explicit_env_overrides={len(explicit_env_wins)} keys={','.join(explicit_env_wins)}")
    else:
        print("[settings validator] explicit_env_overrides=0")

    if args.show_values:
        print("[settings validator] values:")
        for env_name, value in sorted(env_values.items()):
            print(f"  {env_name}={value}")

    print("[settings validator] result=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
