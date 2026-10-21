"""1分足の差分キャッシュ (SUMMARY_BAR_CACHE_MODE)。

背景 (2026-08-17):
  summary_database は push_receiver と別プロセスで global_data.push_df を
  持たないため、毎 tick PUSH DB から 20,000〜30,000 行の tick を読み直し、
  すべて足に組み直していた。実測 job elapsed 51〜58 秒 (tick 間隔 60 秒 /
  timeout 25 秒)。実際の新規行は 1 分あたり 4〜7 行しかない。

  差分キャッシュは完成済みの足を保持し、最後にキャッシュした足以降だけを
  組み直す。既定は off で、shadow / on は env で切り替える。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

import trading.summary.engine.summary_incremental_engine as E
from trading.summary.engine.incremental import bar_cache


def _ticks(symbols=("1301", "6701"), minutes=6, per_min=4, start_cum=100000.0):
    """累計出来高が単調増加する tick 列を作る。"""
    rows = []
    base_dt = dt.datetime(2026, 8, 17, 9, 0)
    for si, sym in enumerate(symbols):
        cum = start_cum + si * 1000
        px = 1000.0 + si * 500
        for m in range(minutes):
            for k in range(per_min):
                cum += 100.0
                px += 0.5
                rows.append({
                    "symbol": sym,
                    "datetime": base_dt + dt.timedelta(minutes=m, seconds=k * 10),
                    "price": px,
                    "volume": cum,          # 累計出来高
                })
    return pd.DataFrame(rows)


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    bar_cache.reset()
    monkeypatch.delenv("SUMMARY_BAR_CACHE_MODE", raising=False)
    monkeypatch.delenv("SUMMARY_BAR_CACHE_MAX_BARS", raising=False)
    yield
    bar_cache.reset()


def test_default_mode_is_off():
    assert bar_cache.get_mode() == "off"


def test_mode_off_does_not_touch_cache(monkeypatch):
    monkeypatch.setenv("SUMMARY_BAR_CACHE_MODE", "off")
    E._build_bars(_ticks(), 1)
    assert bar_cache.cached_rows(1) == 0, "off なのにキャッシュが作られている"


def test_incremental_matches_full_rebuild_on_all_ticks(monkeypatch):
    """当日全 tick から一括構築した足と、差分更新の結果が一致すること。

    volume は「累計出来高の差分」なので、キャッシュ投入後の最初の1本だけは
    前値が無く一致しない (本番の full rebuild も窓の先頭足で同じ欠落がある)。
    ここでは cold start を最初の分に合わせ、それ以降を比較する。
    """
    monkeypatch.setenv("SUMMARY_BAR_CACHE_MODE", "on")
    df = _ticks(minutes=8)
    df["_dt"] = pd.to_datetime(df["datetime"])
    minutes = sorted(df["_dt"].dt.floor("min").unique())

    def _rb(*, interval, ticks, prev_cumulative):
        return E._build_raw_bars(None, interval, ticks=ticks, prev_cumulative=prev_cumulative)

    cached = None
    for i in range(len(minutes)):
        window = df[df["_dt"] <= minutes[i] + pd.Timedelta(minutes=1)].drop(columns=["_dt"])
        cached = bar_cache.build_raw_bars_cached(
            interval=1, ticks=E._normalize_push_df(window), raw_builder=_rb
        )

    oracle = E._build_raw_bars(None, 1, ticks=E._normalize_push_df(df.drop(columns=["_dt"])))

    key = ["symbol", "datetime"]
    merged = oracle[key + ["open", "high", "low", "close", "volume"]].merge(
        cached[key + ["open", "high", "low", "close", "volume"]], on=key, suffixes=("_o", "_c")
    )
    assert not merged.empty

    for col in ("open", "high", "low", "close"):
        assert merged["%s_o" % col].equals(merged["%s_c" % col]), "%s が一致しない" % col

    # volume: 各銘柄の最初の1本を除いて一致すること
    merged = merged.sort_values(key)
    first_idx = merged.groupby("symbol").head(1).index
    rest = merged.drop(index=first_idx)
    assert (rest["volume_o"] - rest["volume_c"]).abs().max() < 1e-9, "先頭以外の volume が一致しない"


def test_prev_cumulative_seed_recovers_first_bar_volume():
    """切り出した tick でも、前の足の累計を渡せば出来高が復元されること。"""
    df = _ticks(symbols=("1301",), minutes=4)
    ticks = E._normalize_push_df(df)

    full = E._build_raw_bars(None, 1, ticks=ticks)
    later = ticks[pd.to_datetime(ticks["datetime"]) >= dt.datetime(2026, 8, 17, 9, 2)]

    # seed 無し: 先頭足の出来高が落ちる
    no_seed = E._build_raw_bars(None, 1, ticks=later)
    # seed 有り: 直前の足の cumulative_volume を渡す
    prev = full[pd.to_datetime(full["datetime"]) < dt.datetime(2026, 8, 17, 9, 2)]
    seed = {"1301": float(prev.sort_values("datetime")["cumulative_volume"].iloc[-1])}
    seeded = E._build_raw_bars(None, 1, ticks=later, prev_cumulative=seed)

    slot = dt.datetime(2026, 8, 17, 9, 2)
    v_full = float(full.loc[pd.to_datetime(full["datetime"]) == slot, "volume"].iloc[0])
    v_no = float(no_seed.loc[pd.to_datetime(no_seed["datetime"]) == slot, "volume"].iloc[0])
    v_seed = float(seeded.loc[pd.to_datetime(seeded["datetime"]) == slot, "volume"].iloc[0])

    assert v_no < v_full, "seed 無しでも落ちていない (前提が崩れている)"
    assert abs(v_seed - v_full) < 1e-9, "seed しても復元できていない (%s != %s)" % (v_seed, v_full)


def test_reset_clears_cache(monkeypatch):
    monkeypatch.setenv("SUMMARY_BAR_CACHE_MODE", "on")
    E._build_bars(_ticks(), 1)
    assert bar_cache.cached_rows(1) > 0
    bar_cache.reset(1)
    assert bar_cache.cached_rows(1) == 0


def test_shadow_returns_full_result(monkeypatch):
    """shadow は従来の結果を返す (キャッシュ側の値を本番へ出さない)。"""
    df = _ticks(minutes=5)
    monkeypatch.setenv("SUMMARY_BAR_CACHE_MODE", "off")
    expected = E._build_bars(df.copy(), 1)

    bar_cache.reset()
    monkeypatch.setenv("SUMMARY_BAR_CACHE_MODE", "shadow")
    got = E._build_bars(df.copy(), 1)

    cols = [c for c in expected.columns if c in got.columns]
    a = expected[cols].sort_values(["symbol", "datetime"]).reset_index(drop=True)
    b = got[cols].sort_values(["symbol", "datetime"]).reset_index(drop=True)
    assert a.equals(b), "shadow が従来と違う結果を返している"


def test_shadow_every_throttles(monkeypatch):
    monkeypatch.setenv("SUMMARY_BAR_CACHE_SHADOW_EVERY", "3")
    got = [bar_cache.shadow_should_compare(1) for _ in range(6)]
    assert got == [False, False, True, False, False, True]
