from __future__ import annotations

import configparser
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SECTION_ALIASES = {
    "ENTRY": ("ENTRY", "entry", "Entry"),
    "SUMMARY_AI": ("SUMMARY_AI", "summary_ai", "SummaryAI", "SUMMARY"),
}

KEY_TO_SECTIONS = {
    "MAX_ENTRY_ONESHOT_YEN": ("ENTRY",),
    "ORDER_LOT_SIZE": ("ENTRY",),
    "ENTRY_MIN_PRICE": ("ENTRY",),
    "ENTRY_MAX_PRICE": ("ENTRY",),
    "ENTRY_AFFORDABILITY_FILTER_ENABLED": ("ENTRY",),
    "ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL": ("ENTRY",),
    "ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS": ("ENTRY",),
    "ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN": ("ENTRY",),
    "ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY": ("ENTRY",),
    "ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN": ("ENTRY",),
    "ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES": ("ENTRY",),
    "SUMMARY_AI_PRE_FILTER_DAILY_RISK": ("SUMMARY_AI", "ENTRY"),
    "SUMMARY_AI_PRE_FILTER_DAILY_RISK_SCOPE": ("SUMMARY_AI", "ENTRY"),
    "SUMMARY_AI_SELL_CREDIT_PREFILTER": ("SUMMARY_AI", "ENTRY"),
}


def _project_root_candidates() -> list[Path]:
    candidates: list[Path] = []

    for env_name in ("PROJECT_ROOT", "KABU_PROJECT_ROOT", "APP_ROOT"):
        v = os.getenv(env_name)
        if v:
            candidates.append(Path(v))

    try:
        here = Path(__file__).resolve()
        candidates.extend([here.parents[2], here.parents[1], Path.cwd()])
    except Exception:
        candidates.append(Path.cwd())

    out: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        try:
            rp = str(p.resolve())
        except Exception:
            rp = str(p)
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def _setting_ini_candidates() -> list[Path]:
    out: list[Path] = []
    direct = os.getenv("SETTING_INI_PATH") or os.getenv("KABU_SETTING_INI")
    if direct:
        out.append(Path(direct))

    for root in _project_root_candidates():
        out.extend([
            root / "setting.ini",
            root / "settings.ini",
            root / "config" / "setting.ini",
            root / "config" / "settings.ini",
            root / "settings" / "setting.ini",
        ])
    return out


@lru_cache(maxsize=1)
def load_setting_ini() -> tuple[configparser.ConfigParser, str]:
    cp = configparser.ConfigParser()
    cp.optionxform = str

    for path in _setting_ini_candidates():
        try:
            if path.exists():
                cp.read(path, encoding="utf-8-sig")
                logger.warning("[SETTING INI] loaded path=%s sections=%s", path, cp.sections())
                return cp, str(path)
        except Exception:
            logger.exception("[SETTING INI] read failed path=%s", path)

    logger.warning("[SETTING INI] not found candidates=%s", [str(p) for p in _setting_ini_candidates()])
    return cp, ""


def get_setting(key: str, default: Any = None) -> Any:
    key_s = str(key).strip()
    if not key_s:
        return default

    cp, _path = load_setting_ini()
    if not cp.sections():
        return default

    section_keys = KEY_TO_SECTIONS.get(key_s, ("ENTRY", "SUMMARY_AI"))
    for section_key in section_keys:
        for section in SECTION_ALIASES.get(section_key, (section_key,)):
            try:
                if cp.has_section(section) and cp.has_option(section, key_s):
                    v = cp.get(section, key_s)
                    if v is not None and str(v).strip() != "":
                        return v
            except Exception:
                pass

    return default


def apply_setting_ini_to_env(*, overwrite: bool = False) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    for key in KEY_TO_SECTIONS.keys():
        v = get_setting(key, None)
        if v is None or str(v).strip() == "":
            continue
        if overwrite or os.getenv(key) is None or str(os.getenv(key)).strip() == "":
            os.environ[key] = str(v).strip()
            applied[key] = str(v).strip()
    if applied:
        logger.warning("[SETTING INI] applied to env overwrite=%s keys=%s", overwrite, sorted(applied.keys()))
    return applied


__all__ = ["load_setting_ini", "get_setting", "apply_setting_ini_to_env"]
