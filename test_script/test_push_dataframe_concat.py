"""PUSH DataFrame への追記で concat の非推奨警告を出さない。

背景 (2026-08-19):
  PUSH のメッセージは板つき/板なしで届くキーが変わり、値が来ない項目は
  None になる。1 行の DataFrame ではその列が「全部 NA」になるため pandas が
    FutureWarning: The behavior of DataFrame concatenation with empty or
    all-NA entries is deprecated.
  を出していた (本番で 1 日 300 件超)。
  将来の pandas では all-NA 列も dtype 判定に参加するので、いま明示して
  結果を固定しておく。
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from trading.push.push_stream.dataframe import _align_all_na_dtypes

TICK = {"symbol": "1301", "price": 100.0, "volume": 1000}
BOARD = {"symbol": "1301", "price": 101.0, "volume": 1100,
         "Sell1Price": 101.5, "Buy1Price": 100.5, "symbolname": "kyokuyo"}
NOVALUE = {"symbol": "1301", "price": None, "volume": None,
           "Sell1Price": None, "Buy1Price": None, "symbolname": None}


def _accumulate(seq, align: bool):
    base = None
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        for r in seq:
            add = pd.DataFrame([r])
            if base is None or base.empty:
                base = add
                continue
            if align:
                add = _align_all_na_dtypes(base, add)
            base = pd.concat([base, add], ignore_index=True)
        n = len([x for x in w if x.category is FutureWarning])
    return base, n


def test_alignment_reduces_future_warnings():
    seq = [TICK, BOARD, NOVALUE] * 6
    _, old_n = _accumulate(seq, align=False)
    _, new_n = _accumulate(seq, align=True)
    assert old_n > 0, "前提が崩れている (旧実装で警告が出ない)"
    assert new_n < old_n, "警告が減っていない"


def test_values_are_unchanged():
    seq = [TICK, BOARD, NOVALUE] * 6
    old, _ = _accumulate(seq, align=False)
    new, _ = _accumulate(seq, align=True)
    assert list(old.columns) == list(new.columns)
    assert old.shape == new.shape
    diff = [c for c in old.columns if not old[c].equals(new[c])]
    assert not diff, "値が変わった列: %s" % diff


def test_non_na_columns_are_untouched():
    base = pd.DataFrame({"a": [1.0, 2.0], "b": ["x", "y"]})
    add = pd.DataFrame([{"a": 3.0, "b": "z"}])
    out = _align_all_na_dtypes(base, add)
    assert out["a"].dtype == add["a"].dtype
    assert out["b"].dtype == add["b"].dtype
    assert out.equals(add)


def test_all_na_column_takes_base_dtype():
    base = pd.DataFrame({"a": pd.Series([1.0, 2.0], dtype="float64")})
    add = pd.DataFrame([{"a": None}])
    assert add["a"].dtype == object
    out = _align_all_na_dtypes(base, add)
    assert out["a"].dtype == np.dtype("float64")
    assert out["a"].isna().all(), "NA が失われている"


def test_new_all_na_column_becomes_float64():
    base = pd.DataFrame({"a": [1.0]})
    add = pd.DataFrame([{"a": 2.0, "newcol": None}])
    out = _align_all_na_dtypes(base, add)
    assert out["newcol"].dtype == np.dtype("float64")
    assert out["newcol"].isna().all()


def test_uncastable_column_is_left_alone():
    """int64 列へ NA を入れられない等の場合でも例外にしない。"""
    base = pd.DataFrame({"a": pd.Series([1, 2], dtype="int64")})
    add = pd.DataFrame([{"a": None}])
    out = _align_all_na_dtypes(base, add)
    assert len(out) == 1, "行が失われている"
    assert out["a"].isna().all()
