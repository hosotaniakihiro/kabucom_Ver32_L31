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
