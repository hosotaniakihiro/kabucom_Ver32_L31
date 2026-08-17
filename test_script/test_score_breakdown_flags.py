"""SCORE_TABLE の flag 突き合わせ (used_flags=0 問題)。

背景 (2026-08-17):
  ログに毎分
    [SCORE BREAKDOWN] SCORE_TABLE loaded flags=180
    [SCORE BREAKDOWN] no matched flag columns; fallback used df_cols=142
  が出ていた。原因は綴りの不一致で、

    score_config.ini 側 : _normalize_flag_key が必ず "flag_" を付ける
                          -> flag_bullish_engulfing
    DataFrame 側        : flag_builder_buy/sell や conditions_long は接頭辞なし
                          -> bullish_engulfing

  そのため列が存在しても一致せず、常に fallback に落ちていた。

  なお本番 pipeline は flag_builder_buy/sell を呼んでおらず、summary DF には
  ma5_above_ma25 / ma5_below_ma25 の 2 個しか無い (builder を通すと 71 個)。
  2 個だけで内訳を組むと fallback より劣化するため、
  一致数が SCORE_BREAKDOWN_MIN_FLAGS 未満なら fallback を維持する。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.scoring.core import score_breakdown as SB


def _base_df(n: int = 20) -> pd.DataFrame:
    idx = pd.date_range("2026-08-17 09:00", periods=n, freq="min")
    return pd.DataFrame({
        "symbol": ["1301"] * n,
        "datetime": idx,
        "close": np.linspace(1000, 1100, n),
        "close_price": np.linspace(1000, 1100, n),
        "volume": np.full(n, 10_000.0),
    })


def test_score_table_keys_are_prefixed():
    table = SB._load_score_table()
    assert table, "SCORE_TABLE が空"
    assert all(k.startswith("flag_") for k in table), "table キーは flag_ 接頭辞で正規化される前提"


def test_bare_column_names_are_matched(monkeypatch):
    """接頭辞なしの列名でも SCORE_TABLE と突き合わせられること。"""
    monkeypatch.setenv("SCORE_BREAKDOWN_MIN_FLAGS", "1")

    df = _base_df()
    table = {"flag_bullish_engulfing": 2.0, "flag_above_vwap": 1.0}
    # DataFrame 側は接頭辞なし (flag_builder_buy/sell の出力と同じ綴り)
    df["bullish_engulfing"] = 1
    df["above_vwap"] = 1

    out = SB.attach_score_breakdown(df.copy(), score_table=table, debug=False)
    total = (
        out["score_base"] + out["score_trend"] + out["score_momentum"]
        + out["score_velocity"] + out["score_penalty"]
    )
    assert (total.abs() > 0).all(), "接頭辞なし列が突き合わせられていない (used_flags=0 のまま)"


def test_prefixed_column_names_still_matched(monkeypatch):
    """従来どおり flag_ 付きの列名でも一致すること。"""
    monkeypatch.setenv("SCORE_BREAKDOWN_MIN_FLAGS", "1")

    df = _base_df()
    table = {"flag_bullish_engulfing": 2.0}
    df["flag_bullish_engulfing"] = 1

    out = SB.attach_score_breakdown(df.copy(), score_table=table, debug=False)
    total = (
        out["score_base"] + out["score_trend"] + out["score_momentum"]
        + out["score_velocity"] + out["score_penalty"]
    )
    assert (total.abs() > 0).all()


def test_low_coverage_keeps_fallback(monkeypatch):
    """一致数が少なすぎるときは fallback を維持する (劣化させない)。"""
    monkeypatch.setenv("SCORE_BREAKDOWN_MIN_FLAGS", "10")

    df = _base_df()
    table = {"flag_f%02d" % i: 1.0 for i in range(180)}
    df["f00"] = 1  # 180 個中 1 個だけ一致

    out = SB.attach_score_breakdown(df.copy(), score_table=table, debug=False)
    fb = SB._fallback_breakdown(df.copy())
    expected_base = pd.to_numeric(fb[0], errors="coerce").fillna(0.0).round(4)
    assert out["score_base"].round(4).tolist() == expected_base.round(4).tolist(), (
        "カバレッジ不足なのに table 方式へ切り替わっている"
    )


def test_min_flags_env_is_read(monkeypatch):
    monkeypatch.setenv("SCORE_BREAKDOWN_MIN_FLAGS", "3")
    assert SB._min_flags_for_table() == 3
    monkeypatch.delenv("SCORE_BREAKDOWN_MIN_FLAGS", raising=False)
    assert SB._min_flags_for_table() == 10
