from __future__ import annotations

import os
from configparser import ConfigParser
from pathlib import Path
from typing import Dict


def _resolve_ini_path(ini_path: str | Path | None = None) -> Path:
    scoring_dir = Path(__file__).resolve().parent
    trading_dir = scoring_dir.parent.parent
    project_root = trading_dir.parent
    candidates: list[Path] = []
    if ini_path:
        candidates.append(Path(ini_path))
    candidates.extend([
        scoring_dir / "score_config.ini",
        trading_dir / "score_config.ini",
        project_root / "score_config.ini",
        project_root / "trading" / "scoring" / "config" / "score_config.ini",
    ])
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("score_config.ini not found. Tried:\n" + "\n".join(str(p) for p in candidates))


def _load_config(ini_path: str | Path | None = None) -> ConfigParser:
    path = _resolve_ini_path(ini_path)
    conf = ConfigParser()
    conf.read(path, encoding="utf-8")
    return conf


def _as_int(v) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "y"}:
            return True
        if s in {"0", "false", "no", "off", "n", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _normalize_key(key: str) -> str:
    return str(key or "").strip().lower()


def _with_flag_prefix(key: str) -> str:
    key = _normalize_key(key)
    if not key:
        return ""
    return key if key.startswith("flag_") else f"flag_{key}"


def _without_flag_prefix(key: str) -> str:
    key = _normalize_key(key)
    return key[5:] if key.startswith("flag_") else key


def _put_with_aliases(table: Dict[str, int], key: str, score: int) -> None:
    key_norm = _normalize_key(key)
    if not key_norm:
        return
    table[key_norm] = int(score)
    table[_with_flag_prefix(key_norm)] = int(score)
    table[_without_flag_prefix(key_norm)] = int(score)


def _read_int_section(conf: ConfigParser, section: str, *, keep_positive: bool | None = None) -> Dict[str, int]:
    table: Dict[str, int] = {}
    if not conf.has_section(section):
        return table
    for key, val in conf[section].items():
        score = _as_int(val)
        if score == 0:
            continue
        if keep_positive is True and score <= 0:
            continue
        if keep_positive is False and score >= 0:
            continue
        _put_with_aliases(table, key, score)
    return table


def _apply_summary_ai_bridge(buy_bonus: Dict[str, int], sell_bonus: Dict[str, int]) -> None:
    if not _env_bool("SUMMARY_AI_SCORE_CONFIG_BRIDGE_EXISTING_SCORE", True):
        return
    buy_points = _env_int("SUMMARY_AI_SCORE_CONFIG_BRIDGE_BUY_POINTS", 4)
    sell_points = _env_int("SUMMARY_AI_SCORE_CONFIG_BRIDGE_SELL_POINTS", 0)
    if buy_points > 0:
        # candidates.py creates this column before score_config scoring.
        buy_bonus.setdefault("ai_disp_buy_score", buy_points)
    if sell_points > 0:
        sell_bonus.setdefault("ai_disp_sell_score", -abs(sell_points))


def build_score_tables(ini_path: str | Path | None = None) -> Dict[str, Dict[str, int] | Dict[str, str]]:
    conf = _load_config(ini_path)

    reentry: Dict[str, str] = {}
    if conf.has_section("reentry"):
        for k, v in conf["reentry"].items():
            key_norm = _normalize_key(k)
            reentry[key_norm] = str(v)
            reentry[_with_flag_prefix(key_norm)] = str(v)
            reentry[_without_flag_prefix(key_norm)] = str(v)

    buy_entry = _read_int_section(conf, "buy_entry", keep_positive=True)
    buy_bonus = _read_int_section(conf, "buy_bonus", keep_positive=True)
    sell_entry = _read_int_section(conf, "sell_entry", keep_positive=False)
    sell_bonus = _read_int_section(conf, "sell_bonus", keep_positive=False)

    if conf.has_section("scoring"):
        for key, val in conf["scoring"].items():
            score = _as_int(val)
            if score <= 0:
                continue
            key_norm = _normalize_key(key)
            target = buy_entry if (
                key_norm.endswith("_event") or key_norm == "dir_up" or key_norm in reentry or _with_flag_prefix(key_norm) in reentry
            ) else buy_bonus
            if key_norm not in target and _with_flag_prefix(key_norm) not in target:
                _put_with_aliases(target, key_norm, score)

    if conf.has_section("short_scoring"):
        for key, val in conf["short_scoring"].items():
            score = _as_int(val)
            if score >= 0:
                continue
            key_norm = _normalize_key(key)
            target = sell_entry if (
                key_norm == "dir_down" or key_norm in reentry or _with_flag_prefix(key_norm) in reentry
            ) else sell_bonus
            if key_norm not in target and _with_flag_prefix(key_norm) not in target:
                _put_with_aliases(target, key_norm, score)

    _apply_summary_ai_bridge(buy_bonus, sell_bonus)

    return {
        "buy_entry": buy_entry,
        "buy_bonus": buy_bonus,
        "sell_entry": sell_entry,
        "sell_bonus": sell_bonus,
        "reentry": reentry,
    }


def build_score_table(ini_path: str | Path | None = None) -> Dict[str, int]:
    conf = _load_config(ini_path)
    table: Dict[str, int] = {}
    for section in ("scoring", "short_scoring", "buy_entry", "buy_bonus", "sell_entry", "sell_bonus"):
        if not conf.has_section(section):
            continue
        for key, val in conf[section].items():
            score = _as_int(val)
            if score == 0:
                continue
            _put_with_aliases(table, key, score)
    if _env_bool("SUMMARY_AI_SCORE_CONFIG_BRIDGE_EXISTING_SCORE", True):
        buy_points = _env_int("SUMMARY_AI_SCORE_CONFIG_BRIDGE_BUY_POINTS", 4)
        if buy_points > 0:
            table.setdefault("ai_disp_buy_score", buy_points)
    return table


TABLES = build_score_tables()
SCORE_TABLE: Dict[str, int] = build_score_table()


if __name__ == "__main__":
    tables = build_score_tables()
    for name, table in tables.items():
        print(f"[{name}] count={len(table)}")
        for k, v in sorted(table.items()):
            print(f"  {k:35s} {v}")
        print()
    print("[SCORE_TABLE]")
    for k, v in sorted(SCORE_TABLE.items()):
        print(f"  {k:35s} {v}")
