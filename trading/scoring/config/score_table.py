# ============================================================
# File   : trading/scoring/config/score_table.py
# Version: Ver28.0-EXPLICIT-ENTRY-BONUS-SECTIONS
# ------------------------------------------------------------
# ✔ score_config.ini を唯一の定義源
# ✔ 旧 [scoring] / [short_scoring] 完全互換
# ✔ 新 [buy_entry] / [buy_bonus] / [sell_entry] / [sell_bonus] を正式読込
# ✔ pattern_dispatcher が期待する TABLES を正しく生成
# ✔ add_scores 用 SCORE_TABLE も新旧セクションを統合
# ✔ flag_ 接頭辞あり/なし両対応
# ✔ cwd 非依存パス解決
# ============================================================

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path
from typing import Dict


# ============================================================
# path / config
# ============================================================

def _resolve_ini_path(ini_path: str | Path | None = None) -> Path:
    scoring_dir = Path(__file__).resolve().parent
    trading_dir = scoring_dir.parent.parent
    project_root = trading_dir.parent

    candidates: list[Path] = []

    if ini_path:
        candidates.append(Path(ini_path))

    candidates.extend(
        [
            scoring_dir / "score_config.ini",
            trading_dir / "score_config.ini",
            project_root / "score_config.ini",
            project_root / "trading" / "scoring" / "config" / "score_config.ini",
        ]
    )

    for p in candidates:
        if p.exists():
            return p

    raise FileNotFoundError(
        "score_config.ini not found. Tried:\n" + "\n".join(str(p) for p in candidates)
    )


def _load_config(ini_path: str | Path | None = None) -> ConfigParser:
    path = _resolve_ini_path(ini_path)
    conf = ConfigParser()
    conf.read(path, encoding="utf-8")
    return conf


# ============================================================
# normalize helpers
# ============================================================

def _as_int(v) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0


def _normalize_key(key: str) -> str:
    return str(key or "").strip().lower()


def _with_flag_prefix(key: str) -> str:
    key = _normalize_key(key)
    if not key:
        return ""
    if key.startswith("flag_"):
        return key
    return f"flag_{key}"


def _without_flag_prefix(key: str) -> str:
    key = _normalize_key(key)
    if key.startswith("flag_"):
        return key[5:]
    return key


def _put_with_aliases(table: Dict[str, int], key: str, score: int) -> None:
    """
    row 側の列名が flag_xxx / xxx のどちらでも拾えるように両方登録する。
    """
    key_norm = _normalize_key(key)
    if not key_norm:
        return

    table[key_norm] = int(score)

    key_flag = _with_flag_prefix(key_norm)
    key_legacy = _without_flag_prefix(key_norm)

    if key_flag:
        table[key_flag] = int(score)
    if key_legacy:
        table[key_legacy] = int(score)


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


# ============================================================
# score tables
# ============================================================

def build_score_tables(ini_path: str | Path | None = None) -> Dict[str, Dict[str, int] | Dict[str, str]]:
    """
    pattern_dispatcher 用のテーブルを返す。

    優先順位:
      1. 新セクション [buy_entry] [buy_bonus] [sell_entry] [sell_bonus]
      2. 旧セクション [scoring] [short_scoring] から不足分を補完

    これにより、score_config.ini に明示的に書いた entry/bonus が
    実際の BUY/SELL 候補スコアに反映される。
    """
    conf = _load_config(ini_path)

    reentry: Dict[str, str] = {}
    if conf.has_section("reentry"):
        for k, v in conf["reentry"].items():
            key_norm = _normalize_key(k)
            reentry[key_norm] = str(v)
            reentry[_with_flag_prefix(key_norm)] = str(v)
            reentry[_without_flag_prefix(key_norm)] = str(v)

    # 新セクションを正式採用
    buy_entry = _read_int_section(conf, "buy_entry", keep_positive=True)
    buy_bonus = _read_int_section(conf, "buy_bonus", keep_positive=True)
    sell_entry = _read_int_section(conf, "sell_entry", keep_positive=False)
    sell_bonus = _read_int_section(conf, "sell_bonus", keep_positive=False)

    # 旧 [scoring] は不足分だけ補完
    if conf.has_section("scoring"):
        for key, val in conf["scoring"].items():
            score = _as_int(val)
            if score <= 0:
                continue
            key_norm = _normalize_key(key)
            target = buy_entry if (
                key_norm.endswith("_event")
                or key_norm == "dir_up"
                or key_norm in reentry
                or _with_flag_prefix(key_norm) in reentry
            ) else buy_bonus
            if key_norm not in target and _with_flag_prefix(key_norm) not in target:
                _put_with_aliases(target, key_norm, score)

    # 旧 [short_scoring] は不足分だけ補完
    if conf.has_section("short_scoring"):
        for key, val in conf["short_scoring"].items():
            score = _as_int(val)
            if score >= 0:
                continue
            key_norm = _normalize_key(key)
            target = sell_entry if (
                key_norm == "dir_down"
                or key_norm in reentry
                or _with_flag_prefix(key_norm) in reentry
            ) else sell_bonus
            if key_norm not in target and _with_flag_prefix(key_norm) not in target:
                _put_with_aliases(target, key_norm, score)

    return {
        "buy_entry": buy_entry,
        "buy_bonus": buy_bonus,
        "sell_entry": sell_entry,
        "sell_bonus": sell_bonus,
        "reentry": reentry,
    }


# ============================================================
# add_scores compatible SCORE_TABLE
# ============================================================

def build_score_table(ini_path: str | Path | None = None) -> Dict[str, int]:
    """
    add_scores.py 互換の単一スコア表。
    新旧すべてのスコアセクションを統合する。
    """
    conf = _load_config(ini_path)

    table: Dict[str, int] = {}
    for section in (
        "scoring",
        "short_scoring",
        "buy_entry",
        "buy_bonus",
        "sell_entry",
        "sell_bonus",
    ):
        if not conf.has_section(section):
            continue
        for key, val in conf[section].items():
            score = _as_int(val)
            if score == 0:
                continue
            _put_with_aliases(table, key, score)

    return table


# ============================================================
# public constants
# ============================================================

TABLES = build_score_tables()
SCORE_TABLE: Dict[str, int] = build_score_table()


# ============================================================
# debug
# ============================================================

if __name__ == "__main__":
    print("\n========= SCORE TABLES =========\n")

    tables = build_score_tables()
    for name, table in tables.items():
        print(f"[{name}] count={len(table)}")
        for k, v in sorted(table.items()):
            print(f"  {k:35s} {v}")
        print()

    print("[SCORE_TABLE / USED BY add_scores]")
    for k, v in sorted(SCORE_TABLE.items()):
        print(f"  {k:35s} {v}")
