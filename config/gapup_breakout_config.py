# ============================================================
# File   : config/gapup_breakout_config.py
# Version: Ver1.0-GAPUP-BREAKOUT
# ------------------------------------------------------------
# ギャップアップ＋初動5分足高値ブレイク 戦略 専用設定
#
#  戦略:
#    1. 寄り付きが前日終値より gap_pct(%) 以上 高い（ギャップアップ）
#    2. 9:00〜9:05 の最初の5分足が確定するまで待つ
#    3. その最初の5分足の高値を記録
#    4. 9:05以降、その高値をティック単位で上抜けた瞬間に成行で買いエントリー
#       （5分足の確定を待たずに発注する）
#
#  ✔ 既存戦略（殿様 validated_signal / summary_ai / ranking / mtf）とは独立
#  ✔ 既定は enabled=1 / live=0（＝ドライラン：検知→ログ/Discord通知のみ）。
#    実発注は GAPUP_BREAKOUT_LIVE=1 で初めて有効化する。
#  ✔ しきい値はすべて ENV で上書き可能（config/early_scalp_config.py と同じ流儀）
# ============================================================

from __future__ import annotations

import configparser
import os
from datetime import time as _time


# ------------------------------------------------------------
# ENV ヘルパ
# ------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "enable", "enabled")


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(float(v)) if v not in (None, "") else int(default)
    except Exception:
        return int(default)


def _env_time(name: str, default: _time) -> _time:
    """ENV の "HH:MM" / "HH:MM:SS" を datetime.time へ。失敗時は default。"""
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        parts = [int(p) for p in str(v).strip().split(":")]
        while len(parts) < 3:
            parts.append(0)
        return _time(parts[0], parts[1], parts[2])
    except Exception:
        return default


def _settings_entry_budget(default: int) -> int:
    """settings.ini [strategy] entry_budget を既定値として流用する。"""
    try:
        conf = configparser.ConfigParser()
        conf.read("settings.ini", encoding="utf-8")
        return conf.getint("strategy", "entry_budget", fallback=default)
    except Exception:
        return default


# ------------------------------------------------------------
# 設定アクセサ（毎 tick 読まれ得るので安い実装にする）
# ------------------------------------------------------------
def is_enabled() -> bool:
    return _env_bool("GAPUP_BREAKOUT_ENABLED", True)


def is_live() -> bool:
    """True のとき実発注。False（既定）はドライラン（通知/ログのみ）。"""
    return _env_bool("GAPUP_BREAKOUT_LIVE", False)


def get_config() -> dict:
    """現在の ENV を反映した設定 dict を返す。"""
    return {
        "enabled": is_enabled(),
        "live": is_live(),

        # ギャップアップ判定（前日終値比 %）
        "gap_pct": _env_float("GAPUP_BREAKOUT_GAP_PCT", 3.0),

        # 初動5分足の窓。[window_start, window_lock) を最初の5分足とし、
        # window_lock 到達で高値を確定（ロック）する。
        "window_start": _env_time("GAPUP_BREAKOUT_WINDOW_START", _time(9, 0, 0)),
        "window_lock": _env_time("GAPUP_BREAKOUT_WINDOW_LOCK", _time(9, 5, 0)),

        # ブレイク監視の打ち切り（既定は前場引け 11:30）。
        "entry_cutoff": _env_time("GAPUP_BREAKOUT_ENTRY_CUTOFF", _time(11, 30, 0)),

        # 発注予算（既定は settings.ini [strategy] entry_budget）。
        "budget": _env_int("GAPUP_BREAKOUT_BUDGET", _settings_entry_budget(500_000)),

        # 前日終値はランキングの PreviousClose を使う（掲載銘柄のみ対象）。
        "require_ranking_prev_close": _env_bool("GAPUP_BREAKOUT_REQUIRE_RANKING_PREV_CLOSE", True),
    }
