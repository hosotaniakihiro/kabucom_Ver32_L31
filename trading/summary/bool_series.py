# ============================================================
# File   : trading/summary/bool_series.py
# ------------------------------------------------------------
# summary 系で多用する「欠損を False とみなして bool にする」変換。
#
# これまで各所で
#     pd.Series(x).fillna(False).astype(bool)
# と書いていたが、summary DF の列は経路によって object dtype になることが
# あり、その場合 fillna が暗黙ダウンキャストになって pandas が
#   FutureWarning: Downcasting object dtype arrays on .fillna, .ffill, .bfill
#   is deprecated
# を出す。実運用ログで継続的に発生していた。
#
# 後段に .infer_objects(copy=False) を足しても警告は消えない
# (警告を出しているのは fillna 自身のため)。非推奨APIである fillna を
# 使わず where で埋めるのが正しい対処で、結果の値は完全に同じ。
# ============================================================

from __future__ import annotations

from typing import Any

import pandas as pd


def na_false_bool(values: Any, index: Any = None) -> pd.Series:
    """欠損を False として bool Series を返す。

    fillna(False).astype(bool) と同じ結果になるが、
    非推奨の暗黙ダウンキャスト経路を通らない。
    """
    try:
        if isinstance(values, pd.Series):
            s = values if index is None else values.reindex(index)
        else:
            s = pd.Series(values, index=index)
        return s.where(s.notna(), False).astype(bool)
    except Exception:
        # 変換できない場合は全 False (従来 astype(bool) が例外になる場面と同じ扱い)
        if index is not None:
            return pd.Series(False, index=index, dtype=bool)
        return pd.Series(dtype=bool)


def na_false_bool_count(values: Any, index: Any = None) -> int:
    """na_false_bool(...).sum() の int 版。"""
    try:
        return int(na_false_bool(values, index=index).sum())
    except Exception:
        return 0
