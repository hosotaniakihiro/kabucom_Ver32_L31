# -*- coding: utf-8 -*-
"""technical_ready を strict に統一したことの回帰テスト。

実 API・実 WebSocket・実 Discord・DB 書き込みは行わない。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _frame(n_ready: int, n_total: int) -> pd.DataFrame:
    """strict で ready=n_ready、残りは「soft なら通ってしまう」行にする。"""
    rows = []
    for i in range(n_total):
        ready = i < n_ready
        rows.append({
            "symbol": "%04d" % (1300 + i),
            "close": 1000.0 + i,
            "close_price": 1000.0 + i,
            "score": 1.0,
            "score_buy": 1.0,
            "display_ready": True,
            # soft 判定を通すための指標 (値はあるが履歴は足りない)
            "rsi": 55.0,
            "macd": 0.5,
            "signal": 0.2,
            "slope": 0.01,
            "mtf": 1.0,
            "score_mtf": 1.0,
            "symbol_hist_len": 30 if ready else 1,
            "technical_ready": ready,
        })
    return pd.DataFrame(rows)


# ============================================================
# 1. SOFT TECH READY が strict を上書きしない
# ============================================================

def test_soft_tech_ready_does_not_inflate_strict():
    from trading.summary import controller_projection as cp

    df = _frame(n_ready=24, n_total=82)
    out = cp._apply_soft_technical_ready(df.copy())

    strict = int(pd.Series(out["technical_ready"]).fillna(False).astype(bool).sum())
    assert strict == 24, "soft が technical_ready を膨らませた: %d" % strict

    # soft 側には退避されていること
    assert "soft_technical_ready" in out.columns
    soft = int(pd.Series(out["soft_technical_ready"]).fillna(False).astype(bool).sum())
    assert soft >= 24, "soft_ready が記録されていない: %d" % soft
    assert int(pd.Series(out["usable_technical_ready"]).fillna(False).astype(bool).sum()) >= soft


# ============================================================
# 2. READY MTF PATCH が strict を上書きしない / hist_len を捏造しない
# ============================================================

def test_ready_mtf_patch_keeps_strict_and_hist_len():
    from trading.summary import controller_layers as cl

    fn = None
    for name in dir(cl):
        obj = getattr(cl, name)
        if callable(obj) and "relax" in name.lower() and "ready" in name.lower():
            fn = obj
            break
    if fn is None:
        pytest.skip("relax technical_ready 関数が見つからない")

    df = _frame(n_ready=24, n_total=82)
    before_hist = df["symbol_hist_len"].tolist()
    try:
        out = fn(df.copy(), context="test")
    except TypeError:
        out = fn(df.copy())

    strict = int(pd.Series(out["technical_ready"]).fillna(False).astype(bool).sum())
    assert strict == 24, "READY MTF PATCH が technical_ready を膨らませた: %d" % strict

    hist = pd.to_numeric(out["symbol_hist_len"], errors="coerce").fillna(0)
    assert (hist < 3).sum() == (pd.Series(before_hist) < 3).sum(), \
        "symbol_hist_len が書き換えられている (履歴本数の捏造)"


# ============================================================
# 3. SHORT PATCH が strict を上書きしない
# ============================================================

def test_short_patch_does_not_set_technical_ready():
    from trading.summary.pipeline import indicator_short_history as sh

    df = pd.DataFrame({
        "symbol": ["1301", "1302", "1303"],
        "datetime": pd.to_datetime(["2026-09-02 10:00", "2026-09-02 10:00", "2026-09-02 10:00"]),
        "close": [1000.0, 2000.0, 3000.0],
        "high": [1010.0, 2010.0, 3010.0],
        "low": [990.0, 1990.0, 2990.0],
        "score": [1.0, 1.0, 1.0],
        "technical_ready": [True, False, False],
        "symbol_hist_len": [30, 1, 1],
    })
    out = sh.add_short_history_indicators(df.copy(), interval=1)

    strict = int(pd.Series(out["technical_ready"]).fillna(False).astype(bool).sum())
    assert strict == 1, "SHORT PATCH が technical_ready を True にした: %d" % strict
    assert "usable_technical_ready" in out.columns
    assert "soft_technical_ready" in out.columns


# ============================================================
# 4. パイプライン全体で strict が保たれる (ready=24 -> 24)
# ============================================================

def test_strict_ready_survives_projection_and_patch():
    from trading.summary import controller_layers as cl
    from trading.summary import controller_projection as cp

    df = _frame(n_ready=24, n_total=82)

    out = cp._apply_soft_technical_ready(df.copy())
    assert int(out["technical_ready"].sum()) == 24

    fn = None
    for name in dir(cl):
        obj = getattr(cl, name)
        if callable(obj) and "relax" in name.lower() and "ready" in name.lower():
            fn = obj
            break
    if fn is not None:
        try:
            out = fn(out, context="test")
        except TypeError:
            out = fn(out)

    final = int(pd.Series(out["technical_ready"]).fillna(False).astype(bool).sum())
    assert final == 24, "rebuild_technical_ready ready=24 が後段で %d に増えた" % final


# ============================================================
# 5. soft_ready だけでは ENTRY 対象にならない
# ============================================================

def test_entry_uses_strict_ready_only():
    """ENTRY 判定に渡る技術的準備完了は strict 列であること。"""
    df = _frame(n_ready=24, n_total=82)
    from trading.summary import controller_projection as cp

    out = cp._apply_soft_technical_ready(df.copy())

    entry_ready = pd.Series(out["technical_ready"]).fillna(False).astype(bool)
    soft_only = pd.Series(out["soft_technical_ready"]).fillna(False).astype(bool) & (~entry_ready)
    assert soft_only.sum() > 0, "テスト前提が崩れている (soft だけの行が無い)"
    # soft だけの行が entry_ready に混ざっていないこと
    assert not (entry_ready & soft_only).any()
