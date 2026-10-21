"""ma5_slope / ma25_slope の生成 (missing col=ma25_slope 問題)。

背景 (2026-08-17):
  毎分 [ranking_pipeline][pre-latest] missing col=ma5_slope / ma25_slope が出ていた。
  この 2 列は
    - ranking_pipeline._ensure_trend の優先度2 (trend <- ma5_slope + ma25_slope)
    - trading/ranking/features/mtf_score.py
    - trading/ranking/scoring/score_builder.py
  が参照しているのに、ranking 経路のどこも作っていなかった (消費側だけ存在)。
  ranking DB の ma5_slope_1m / ma25_slope_1m も全 NULL。

  さらに summary_controller が run_ranking_pipeline に df_latest
  (銘柄あたり1行) を渡していたため、実測 history stats min=1 max=1
  short(<3)=204/204 となり、diff()/rolling() 由来の特徴量は構造的に 0 だった。
  ranking_pipeline は内部で select_latest_rows() する設計なので履歴を渡す。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading.ranking.features.slope import ensure_ma_slopes


def _hist(symbols=("1301", "6701"), n=10) -> pd.DataFrame:
    rows = []
    for si, sym in enumerate(symbols):
        base = 1000.0 + si * 500
        for k in range(n):
            rows.append({
                "symbol": sym,
                "datetime": pd.Timestamp("2026-08-17 09:00") + pd.Timedelta(minutes=k),
                "close": base + k * 2.0,
                "ma5": base + k * 1.5,
                "ma25": base + k * 0.8,
            })
    return pd.DataFrame(rows)


def test_ma_slopes_are_created_from_history():
    df = _hist()
    assert "ma5_slope" not in df.columns
    out = ensure_ma_slopes(df)

    assert "ma5_slope" in out.columns
    assert "ma25_slope" in out.columns
    # 単調増加の ma なので傾きは正の値を持つ (全ゼロ/全NULLではない)
    for col in ("ma5_slope", "ma25_slope"):
        s = pd.to_numeric(out[col], errors="coerce")
        assert s.notna().any(), "%s が全 NULL" % col
        assert s.ne(0).any(), "%s が全ゼロ" % col
        assert (s.dropna() >= 0).all(), "%s の符号が単調増加と矛盾" % col


def test_single_row_per_symbol_cannot_produce_slope():
    """1銘柄1行だと構造的に傾きが出ない (本番で起きていた状態)。"""
    df = _hist(n=1)
    out = ensure_ma_slopes(df)
    for col in ("ma5_slope", "ma25_slope"):
        s = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
        assert s.eq(0).all(), "1行しかないのに %s が非ゼロ" % col


def test_existing_columns_are_not_overwritten():
    df = _hist()
    df["ma25_slope"] = 12345.0
    out = ensure_ma_slopes(df)
    assert (pd.to_numeric(out["ma25_slope"], errors="coerce") == 12345.0).all()


def test_missing_source_column_does_not_fabricate_zero():
    """元になる ma25 が無いときに 0 で埋めて「値がある」ふりをしないこと。"""
    df = _hist().drop(columns=["ma25"])
    out = ensure_ma_slopes(df)
    assert "ma5_slope" in out.columns
    assert "ma25_slope" not in out.columns


def test_pipeline_declares_ma_slopes_before_diagnostics():
    """ranking_pipeline が診断より前に ensure_ma_slopes を呼ぶこと。"""
    import inspect

    from trading.ranking import ranking_pipeline as RP

    src = inspect.getsource(RP.run_ranking_pipeline)
    i_make = src.find("ensure_ma_slopes(")
    i_probe = src.find("[ranking_pipeline][pre-latest]")
    assert i_make >= 0, "ensure_ma_slopes が呼ばれていない"
    assert i_probe >= 0
    assert i_make < i_probe, "診断が生成より先にある (missing と出続ける)"
