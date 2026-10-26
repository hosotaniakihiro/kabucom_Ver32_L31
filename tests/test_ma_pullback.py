"""5 分足 25MA 押し目買い (記録だけ) の形の判定 (2026-10-26)。"""
from trading.strategies.ma_pullback import _to5, cfg, detect


def _bars(closes, lows=None, highs=None, opens=None):
    out = []
    for k, c in enumerate(closes):
        h = highs[k] if highs else c
        l = lows[k] if lows else c
        o = opens[k] if opens else c
        out.append([f"{9 + (k * 5) // 60:02d}:{(k * 5) % 60:02d}", o, h, l, c])
    return out


def _rally_then_pullback(last_open, last_close, last_low):
    # 25 本 1000 円で横ばい → 12 本で 1080 円まで上げる → 25MA まで下りる
    closes = [1000.0] * 25 + [1000.0 + 8 * k for k in range(1, 11)] + [1060.0, 1030.0]
    prev, today = closes[:25], closes[25:]
    prev_b = _bars(prev)
    today_b = _bars(today)
    today_b.append(["10:00", last_open, max(last_open, last_close), last_low, last_close])
    return prev_b, today_b


def test_bullish_touch_after_far_is_signal():
    prev, today = _rally_then_pullback(1018.0, 1026.0, 1015.0)
    sig = detect(prev, today, len(today) - 1, cfg())
    assert sig is not None
    assert sig["far_pct"] >= 2.0
    assert sig["close"] > sig["ma25"]


def test_bearish_touch_is_not_signal():
    prev, today = _rally_then_pullback(1030.0, 1022.0, 1015.0)
    assert detect(prev, today, len(today) - 1, cfg()) is None


def test_no_touch_is_not_signal():
    prev, today = _rally_then_pullback(1040.0, 1050.0, 1038.0)
    assert detect(prev, today, len(today) - 1, cfg()) is None


def test_not_enough_bars():
    prev, today = _rally_then_pullback(1018.0, 1026.0, 1015.0)
    assert detect([], today, len(today) - 1, cfg()) is None


def test_to5_uses_first_close_as_open():
    rows = [("1", "2026-10-26 09:00:00", None, 101, 99, 100),
            ("1", "2026-10-26 09:01:00", 100, 105, 100, 104),
            ("1", "2026-10-26 09:05:00", 104, 104, 103, 103)]
    b = _to5(rows)["1"]
    assert b[0] == ["09:00", 100.0, 105.0, 99.0, 104.0]
    assert b[1][0] == "09:05"


# ---- 発注と決済 (2026-10-26 夜) ----
import datetime as dt  # noqa: E402

import trading.strategies.ma_pullback as mp  # noqa: E402

SIG = {"ma25": 1000.0, "far_pct": 3.5, "close": 1005.0, "open": 1000.0, "high": 1006.0, "low": 1001.0}
NOW = dt.datetime(2026, 10, 27, 10, 0, 20)


def _patch_order(monkeypatch, bid, ask):
    sent = []
    monkeypatch.setattr(mp, "_board", lambda s: {"bid": bid, "ask": ask})
    import trading.handlers.entry_handler as eh
    monkeypatch.setattr(eh, "place_entry_buy", lambda *a, **k: sent.append((a, k)) or "OID1")
    return sent


def test_try_entry_orders_with_lot(monkeypatch):
    sent = _patch_order(monkeypatch, 1004.0, 1005.0)
    st = {"orders": {}}
    res, why, d = mp._try_entry("1234", SIG, st, NOW, mp.cfg())
    assert (res, why) == ("ordered", "ok")
    assert sent[0][1]["qty"] == 400 and sent[0][0][2] == 1005.0   # 50 万円 / 1005 円 -> 400 株
    assert st["orders"]["1234"]["order_id"] == "OID1"
    assert mp._try_entry("1234", SIG, st, NOW, mp.cfg())[1] == "already_ordered_today"


def test_try_entry_skips_wide_spread_and_bounce(monkeypatch):
    sent = _patch_order(monkeypatch, 1000.0, 1005.0)
    assert mp._try_entry("1", SIG, {"orders": {}}, NOW, mp.cfg())[1] == "spread_too_wide"
    _patch_order(monkeypatch, 1015.0, 1016.0)
    assert mp._try_entry("1", SIG, {"orders": {}}, NOW, mp.cfg())[1] == "already_bounced"
    assert not sent


def test_handle_exit_time_and_not_mine(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    st = {"done": {}, "trades": [], "orders": {"1234": {"order_ts": "2026-10-27 10:00:20", "order_id": "X"}}}
    monkeypatch.setattr(mp, "_load", lambda: st)
    monkeypatch.setattr(mp, "_save", lambda s: None)
    import trading.exit.exit_position_runner as epr
    import trading.exit.exit_price_source as eps
    import trading.exit.exit_finalize as ef
    monkeypatch.setattr(epr, "_resolve_entry_price", lambda p: 1005.0)
    monkeypatch.setattr(eps, "get_latest_exit_price", lambda s: (1010.0, {}))
    done = []
    monkeypatch.setattr(ef, "finalize_exit", lambda **k: done.append(k))
    pos = {"side": "BUY"}
    assert mp.handle_exit("9999", pos, NOW) is None
    assert mp.handle_exit("1234", pos, NOW + dt.timedelta(minutes=10)) is False
    assert mp.handle_exit("1234", pos, NOW + dt.timedelta(minutes=31)) is True
    assert done[0]["reason"].startswith("MA_PULLBACK_TIME")
    assert st["orders"]["1234"]["closed"] is True
