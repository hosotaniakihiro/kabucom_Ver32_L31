"""na_false_bool が fillna(False).astype(bool) と等価であること。

背景 (2026-08-17):
  summary 系の 17 箇所が pd.Series(x).fillna(False).astype(bool) を使っており、
  列が object dtype になる経路で
    FutureWarning: Downcasting object dtype arrays on .fillna, .ffill, .bfill
  が本番ログに継続的に出ていた。

  後段に .infer_objects(copy=False) を付けても消えない
  (警告を出しているのは fillna 自身)。非推奨 API を使わず where で埋める。
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from trading.summary.bool_series import na_false_bool, na_false_bool_count

CASES = [
    ("object 混在", pd.Series([1, 0, None, True, False, np.nan], dtype="object")),
    ("object 全NaN", pd.Series([None, None, None], dtype="object")),
    ("object 文字列", pd.Series(["1", "", None, "0"], dtype="object")),
    ("float", pd.Series([1.0, 0.0, np.nan])),
    ("int", pd.Series([1, 0, 2])),
    ("bool", pd.Series([True, False, True])),
    ("空", pd.Series([], dtype="object")),
]


@pytest.mark.parametrize("label,s", CASES, ids=[c[0] for c in CASES])
def test_matches_legacy_fillna_astype(label: str, s: pd.Series):
    expected = pd.Series(s).fillna(False).astype(bool)
    got = na_false_bool(s)
    assert got.reset_index(drop=True).equals(expected.reset_index(drop=True)), (
        "%s で結果が変わった: 旧=%s 新=%s" % (label, list(expected), list(got))
    )


@pytest.mark.parametrize("label,s", CASES, ids=[c[0] for c in CASES])
def test_does_not_use_deprecated_fillna_downcast(label: str, s: pd.Series):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        na_false_bool(s)
    future = [x for x in w if x.category is FutureWarning]
    assert not future, "%s で FutureWarning: %s" % (label, [str(x.message)[:80] for x in future])


def test_count_helper():
    s = pd.Series([1, 0, None, True], dtype="object")
    assert na_false_bool_count(s) == 2


def test_reindex_by_index():
    s = pd.Series([True, False], index=[10, 11])
    out = na_false_bool(s, index=[10, 11, 12])
    assert list(out) == [True, False, False]
    assert list(out.index) == [10, 11, 12]


def test_scalar_broadcast_matches_legacy():
    idx = pd.RangeIndex(3)
    expected = pd.Series(False, index=idx).fillna(False).astype(bool)
    assert na_false_bool(False, index=idx).equals(expected)


def test_call_sites_do_not_use_fillna_false():
    """置換漏れの検出。summary 系で fillna(False).astype(bool) を復活させない。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    targets = [
        "trading/summary/controller_cache.py",
        "trading/summary/controller_cache_safe_set.py",
        "trading/summary/controller_utils.py",
        "trading/summary/pipeline/indicator_short_history.py",
    ]
    offenders = []
    for rel in targets:
        p = root / rel
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if ".fillna(False)" in line and ".astype(bool)" in line:
                offenders.append("%s:%d" % (rel, i))
    assert not offenders, "fillna(False).astype(bool) が残っている:\n  " + "\n  ".join(offenders)
