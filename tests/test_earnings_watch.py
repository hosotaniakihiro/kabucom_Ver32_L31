"""場中の決算直後の記録 (2026-10-26)。"""
import datetime as dt

from trading.monitors.earnings_watch import evaluate, in_session_event


def test_in_session_event():
    assert in_session_event(dt.datetime(2026, 10, 27, 13, 0), "2027年3月期 第2四半期決算短信")
    assert not in_session_event(dt.datetime(2026, 10, 27, 15, 30), "決算短信")
    assert not in_session_event(dt.datetime(2026, 10, 27, 12, 0), "決算短信")
    assert not in_session_event(dt.datetime(2026, 10, 27, 13, 0), "自己株式の取得状況")


def test_evaluate_fade_after_jump():
    t0 = dt.datetime(2026, 10, 27, 13, 0)
    px = {"12:59": 1000.0, "13:00": 1030.0, "13:01": 1040.0, "13:02": 1050.0}
    px.update({f"13:{m:02d}": 1020.0 for m in range(3, 36)})
    r = evaluate(t0, px)
    assert r["base"] == 1000.0 and r["chg_1m"] == 4.0
    # k=1: 1040 で逆張り (13:02 の 1050 で売り) -> 13:17 の 1020 で返す
    assert abs(r["fade15_k1"] - ((1 - 1020 / 1050) * 100 - 0.15)) < 1e-3


def test_evaluate_small_move_no_trade():
    t0 = dt.datetime(2026, 10, 27, 13, 0)
    px = {f"{12 + (59 + m) // 60}:{(59 + m) % 60:02d}": 1000.0 + m for m in range(0, 40)}
    r = evaluate(t0, px)
    assert "fade15_k1" not in r and "fade15_k2" not in r
