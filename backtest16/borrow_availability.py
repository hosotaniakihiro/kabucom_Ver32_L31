# ============================================================
# backtest16/borrow_availability.py
# ------------------------------------------------------------
# 信用売建可否の区分(§6.1)。
#
#  重要な事実(本システムの実データ制約):
#   - 過去日の「制度/一般信用の売建可否・売建可能数量・新規売建停止」を
#     日次スナップショットとして保存する仕組みは存在しない(本番はkabu APIの
#     リアルタイム判定のみ)。したがってバックテストでは当時の実在庫・発注可否を
#     確認できない。
#   - symbol_flags.db に short_sellable 列(粗い適格性)がある場合はそれを B 相当の
#     手掛かりに使うが、当日の在庫・規制は不明のため基本は D(適格性不明)に倒す。
#
#  区分:
#   A: 当時の売建可能性と数量を確認済み → 実行可能性確認済みに算入
#   B: 銘柄/信用区分として適格だが当時の在庫・発注可否は不明 → 仮想
#   C: 売建不可が確認済み → 売買不可として記録
#   D: 適格性自体が不明 → 仮想
# ============================================================

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from functools import lru_cache
from typing import Optional

from backtest16 import config as C

# symbol_flags.db の候補パス(存在すれば short_sellable を参照)
_SYMBOL_FLAGS_CANDIDATES = [
    os.path.join(C.DATA_ROOT, "..", "symbol_flags.db"),
    os.path.join(C.DATA_ROOT, "symbol_flags.db"),
    r"X:/symbol_flags.db",
]


@lru_cache(maxsize=1)
def _load_short_sellable_flags() -> dict:
    """symbol_flags.db から {symbol: short_sellable(bool)} を読む。無ければ空。
    注意: これは「現在の」フラグで、当時の日次状態ではない(→区分Bどまり)。"""
    for path in _SYMBOL_FLAGS_CANDIDATES:
        p = os.path.normpath(path)
        if not os.path.exists(p):
            continue
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=5.0)
            try:
                tbls = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                for t in tbls:
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
                    if "symbol" in cols and ("short_sellable" in cols or "sell" in cols):
                        col = "short_sellable" if "short_sellable" in cols else "sell"
                        rows = conn.execute(f'SELECT symbol, "{col}" FROM "{t}"').fetchall()
                        return {str(s): bool(v) for s, v in rows if s is not None}
            finally:
                conn.close()
        except Exception:
            continue
    return {}


def classify(symbol: str, date: dt.date, *, short_sellable_hint: Optional[bool] = None) -> str:
    """当該銘柄・日の信用売建可否区分を返す(A/B/C/D)。
    当時の履歴データが無いため A は原則出せない。"""
    flags = _load_short_sellable_flags()
    hint = short_sellable_hint
    if hint is None and symbol in flags:
        hint = flags[symbol]
    if hint is True:
        return "B"   # 適格の手掛かりあり・当時の在庫/規制は不明
    if hint is False:
        return "C"   # 売建不可の手掛かり
    return "D"       # 適格性自体が不明


def is_executable_class(klass: str) -> bool:
    """実行可能性確認済み(=実損益に算入してよい)のは A のみ(§6.1)。"""
    return klass == "A"


def data_availability_note() -> str:
    return ("過去の信用売建可否・売建可能数量の日次履歴が存在しないため、"
            "本バックテストの売建可否は区分B/Dが中心(当時の在庫・発注可否は未確認)。"
            "区分Aは出せないため、空売りの実損益は『仮想シグナル成績』として扱う。")
