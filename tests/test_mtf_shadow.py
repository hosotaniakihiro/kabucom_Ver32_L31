"""方向 AI (記録だけ) の特徴量・ラベル・場中記録 (2026-10-30)。"""
import datetime as dt

import numpy as np
import pandas as pd

from AI.mtf_shadow import runtime
from AI.mtf_shadow.features import FEATURES, build_features, build_label, horizon_bars
from trading.summary.mtf_local_bars import _add_indicators


def _bars(days=2, n=60, tf=1, start_price=1000.0):
    rows = []
    rng = np.random.default_rng(0)
    p = start_price
    for d in range(days):
        t0 = dt.datetime(2026, 10, 1 + d, 9, 0)
        for k in range(n):
            o = p
            p = max(1.0, p + rng.integers(-3, 4))
            rows.append({"datetime": t0 + dt.timedelta(minutes=tf * k), "open": o,
                         "high": max(o, p) + 1, "low": min(o, p) - 1, "close": p, "volume": 0.0})
    return _add_indicators(pd.DataFrame(rows))


def test_features_columns_and_day_bounded_returns():
    b = _bars()
    f = build_features(b)
    assert list(f.columns) == FEATURES
    assert len(f) == len(b)
    # 2 日目の先頭の足は前日の終値と比べない (場中の get_bars は当日分しか無い)
    assert np.isnan(f["ret1"].iloc[60])
    assert not np.isnan(f["ret1"].iloc[61])
    assert f["tod"].iloc[0] == 0 and f["tod"].iloc[59] == 59


def test_label_stays_inside_the_day():
    b = _bars(tf=3)
    y = build_label(b, 3)
    h = horizon_bars(3)
    assert h == 5
    assert y.iloc[60 - h:60].isna().all()      # 1 日目の最後の h 本は翌日を見ない
    assert y.iloc[: 60 - h].notna().all()
    assert set(y.dropna().unique()) <= {0.0, 1.0, 2.0}


def test_record_ignores_junk_and_throttles(monkeypatch):
    monkeypatch.setattr(runtime, "_ensure_worker", lambda: None)
    while not runtime._q.empty():
        runtime._q.get_nowait()
    runtime._last_sent.clear()
    runtime.record(None, {})
    runtime.record({"symbol": "7203"}, {})                 # 向きが無い
    runtime.record({"symbol": "7203", "side": "BUY"}, None)
    runtime.record({"symbol": "7203", "side": "BUY"}, {"allow": True})  # 60 秒以内は捨てる
    runtime.record({"symbol": "7203", "entry_decision": "SELL"}, {"allow": False})
    assert runtime._q.qsize() == 2
    monkeypatch.setenv("MTF_SHADOW_ENABLED", "0")
    runtime.record({"symbol": "6758", "side": "BUY"}, {})
    assert runtime._q.qsize() == 2


class _FakeModel:
    def predict_proba(self, X):
        assert list(X.columns) == FEATURES
        return np.array([[0.2, 0.3, 0.5]] * len(X))


def test_handle_and_resolve_write_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    now = dt.datetime(2026, 10, 1, 9, 40, 30)
    bars = _bars(days=1, n=60)
    bars["completed"] = (bars["datetime"] + pd.Timedelta(minutes=1)) <= now

    import trading.summary.mtf_local_bars as mlb
    monkeypatch.setattr(mlb, "get_bars", lambda s, tf, now=None: bars)
    monkeypatch.setattr(mlb, "is_stale", lambda b, tf, now=None, max_age_bars=2.0: False)
    monkeypatch.setattr(runtime, "_models", {1: {"model": _FakeModel(), "features": FEATURES}})

    conn = runtime._db(now.date())
    runtime._handle(conn, {"ts": now, "symbol": "7203", "side": "BUY", "source": "SUMMARY",
                           "interval": 1, "allow": 1, "reason": "x"})
    r = conn.execute("SELECT bar1_dt, close1, p1_up, p3_up, fwd_ret FROM records").fetchone()
    assert r[0].startswith("2026-10-01T09:39")          # 確定した最後の 1 分足
    assert r[2] == 0.5 and r[3] is None and r[4] is None

    bars["completed"] = True
    runtime._resolve(conn, dt.datetime(2026, 10, 1, 9, 57))
    c0 = float(bars.loc[bars["datetime"] == pd.Timestamp("2026-10-01 09:39"), "close"].iloc[0])
    c1 = float(bars.loc[bars["datetime"] == pd.Timestamp("2026-10-01 09:54"), "close"].iloc[0])
    fwd = conn.execute("SELECT fwd_ret FROM records").fetchone()[0]
    assert abs(fwd - (c1 / c0 - 1)) < 1e-12
    conn.close()
    assert (tmp_path / "autostock" / "ai_shadow" / "mtf_shadow_20261001.db").exists()
