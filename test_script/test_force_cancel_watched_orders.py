# 2026-10-23: 新規注文の見張りが受け持つ注文は、期限 + 猶予まで force_cancel_loop が消さない
import time

import pytest

import force_cancel_loop as FC
from trading.handlers import pending_order_monitor as PM


class _Res:
    def __init__(self, orders):
        self.ok, self.orders, self.source, self.error = True, orders, "test", None


class _Stop(Exception):
    pass


@pytest.fixture
def run_once(monkeypatch):
    po = {}
    monkeypatch.setattr(PM, "_ensure_pending_orders", lambda: po)
    cancelled = []
    monkeypatch.setattr(FC, "cancel_order", lambda oid: cancelled.append(oid))
    monkeypatch.setattr(FC.orders_read_cache, "invalidate", lambda **k: None)

    def sleep(sec):
        if sec >= 1:
            raise _Stop
    monkeypatch.setattr(FC.time, "sleep", sleep)

    def run(orders):
        monkeypatch.setattr(FC, "get_orders_state", lambda: _Res(orders))
        with pytest.raises(_Stop):
            FC.start_force_cancel_loop(interval_sec=30)
        return cancelled
    return po, run


def _o(oid):
    return {"OrderId": oid, "State": 3, "Price": 100.0, "CumQty": 0, "OrderQty": 100}


def test_watched_order_kept_until_deadline(run_once):
    po, run = run_once
    now = time.time()
    po["W1"] = {"created_at": now - 5, "cancel_after_sec": 60.0}           # 待ち中
    po["W2"] = {"created_at": now - 100, "cancel_after_sec": 60.0}         # 期限+猶予を過ぎた
    po["W3"] = {"created_at": now - 5, "cancel_after_sec": 60.0, "cancel_requested": True}
    assert run([_o("W1"), _o("W2"), _o("W3"), _o("X")]) == ["W2", "W3", "X"]


def test_deadline_helper():
    assert PM.watched_order_deadline("nope") is None
