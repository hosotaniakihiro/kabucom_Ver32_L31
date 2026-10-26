"""pending_monitor が pending_order_monitor の見張り行を消さない (2026-10-26)。"""
import datetime as dt
import threading
import time

from global_state import global_data
from trading.handlers import pending_monitor as pm


class _Res:
    ok = True
    orders = []


def test_watched_rows_survive_one_loop(monkeypatch):
    setattr(global_data, "pending_orders", {
        "W1": {"symbol": "6981", "created_at": time.time(), "cancel_after_sec": 60.0, "mode": None},
        "BAD": {"symbol": "1111"},  # timestamp も created_at も無い行は従来どおり消す
    })
    monkeypatch.setattr(pm, "get_orders_state", lambda: _Res())
    calls = {"n": 0}

    def _sleep(_s):
        calls["n"] += 1
        if calls["n"] >= 1:
            raise SystemExit  # 1 周で止める

    monkeypatch.setattr(pm.time, "sleep", _sleep)
    started = []
    monkeypatch.setattr(pm.threading, "Thread", lambda target, **k: started.append(target) or type("T", (), {"start": lambda self: None})())
    pm.start_pending_monitor()
    try:
        started[0]()
    except SystemExit:
        pass
    po = getattr(global_data, "pending_orders")
    assert "W1" in po and "BAD" not in po
