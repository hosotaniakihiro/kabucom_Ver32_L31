"""rotation_stability._drop_over_max_price の確認 (2026-10-26)。"""
import datetime as dt

from trading.push.push_stream import rotation_stability as rs

NOW = dt.datetime(2026, 10, 26, 12, 40)
PRICES = {"6857": 31280.0, "285A": 12475.0, "6525": 6523.0, "7000": 7000.0}


def _setup(monkeypatch, protected=()):
    monkeypatch.setattr(rs, "_latest_price_map", lambda syms, now: {s: PRICES[s] for s in syms if s in PRICES})
    from trading.push.push_stream import protected_symbols

    monkeypatch.setattr(protected_symbols, "resolve_protected_push_symbols", lambda: list(protected))
    monkeypatch.setenv("SUMMARY_PIPELINE_MAX_PRICE", "7000")


def test_drops_over_max_keeps_unknown_and_boundary(monkeypatch):
    _setup(monkeypatch)
    out = rs._drop_over_max_price(["6857", "6525", "9999", "285A", "7000"], NOW)
    assert out == ["6525", "9999", "7000"]


def test_protected_stays(monkeypatch):
    _setup(monkeypatch, protected=("6857",))
    assert rs._drop_over_max_price(["6857", "285A", "6525"], NOW) == ["6857", "6525"]


def test_disabled_and_override(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setenv("PUSH_ROTATION_PRICE_FILTER", "0")
    assert rs._drop_over_max_price(["6857", "6525"], NOW) == ["6857", "6525"]
    monkeypatch.setenv("PUSH_ROTATION_PRICE_FILTER", "1")
    monkeypatch.setenv("PUSH_ROTATION_MAX_PRICE", "20000")
    assert rs._drop_over_max_price(["6857", "285A", "6525"], NOW) == ["285A", "6525"]


def test_burst_symbols_from_ranking_db(tmp_path, monkeypatch):
    """3 分で ±1.5% 以上 + 売買代金 1 億円以上だけ、変化の大きい順 (2026-10-26)。"""
    import sqlite3
    from trading.ranking import ranking_db_location

    db = tmp_path / "ranking.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ranking_snapshot_1min (symbol TEXT, datetime TEXT, price REAL, current_price REAL, trading_value REAL)")
    data = {
        "6203": [(1952, 5.8e8), (1979, 6.7e8), (2010, 8.2e8)],  # +3.0%, +2.4 億
        "1111": [(1000, 1e9), (1000, 1.5e9), (1020, 2e9)],      # +2.0%, +10 億
        "2222": [(1000, 1e9), (1010, 1.0e9), (1030, 1.05e9)],   # +3.0% だが代金 0.5 億
        "3333": [(1000, 1e9), (1005, 2e9), (1010, 3e9)],        # +1.0%
        "4444": [(1000, 1e9), (990, 2e9), (970, 3e9)],          # -3.0% (下げも拾う)
    }
    for sym, pts in data.items():
        for k, (p, v) in enumerate(pts):
            con.execute("INSERT INTO ranking_snapshot_1min VALUES (?,?,?,?,?)", (sym, f"2026-10-26 10:{48 + k * 1 + (1 if k == 2 else 0):02d}:00", p, p, v))
    con.commit(); con.close()
    monkeypatch.setattr(ranking_db_location, "ranking_db_path", lambda d: db)
    rs._BURST_CACHE.update(at=0.0, key="", syms=[])
    out = rs._recent_burst_symbols(dt.datetime(2026, 10, 26, 10, 51, 20))
    assert out == ["4444", "6203", "1111"] or out == ["6203", "4444", "1111"]
    monkeypatch.setenv("PUSH_ROTATION_BURST_PRIORITY", "0")
    assert rs._recent_burst_symbols(dt.datetime(2026, 10, 26, 10, 51, 20)) == []
