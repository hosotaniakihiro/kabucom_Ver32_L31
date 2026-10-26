# 2026-10-23: 判定価格からのずれ — 不利な向きは 0.2%、有利な向きは 1.5% で止める
import pytest

from trading.handlers import entry_order_builder as EB


@pytest.fixture
def board(monkeypatch):
    monkeypatch.delenv("ENTRY_PRICE_DRIFT_MAX_PCT", raising=False)
    monkeypatch.delenv("ENTRY_PRICE_DRIFT_FAVORABLE_MAX_PCT", raising=False)

    def set_px(px):
        monkeypatch.setattr(EB, "_get_board_with_retry", lambda *a, **k: {"bid_price": px, "ask_price": px})
    return set_px


def guard(side, judged=1000.0):
    return EB._price_drift_guard(symbol="1234", side=side, source="SUMMARY_AI", entry_row={"close_price": judged})


@pytest.mark.parametrize("side,px,blocked", [
    ("BUY", 1001.5, False), ("BUY", 1003.0, True),      # 不利 (上): 0.2% まで
    ("BUY", 990.0, False), ("BUY", 984.0, True),        # 有利 (下): 1.5% まで (10/23 朝の 4498 -0.97% は通る)
    ("SELL", 998.5, False), ("SELL", 997.0, True),      # 不利 (下)
    ("SELL", 1010.0, False), ("SELL", 1016.0, True),    # 有利 (上)
])
def test_sides(board, side, px, blocked):
    board(px)
    assert (guard(side) is not None) is blocked


def test_favorable_env(board, monkeypatch):
    board(990.0)
    monkeypatch.setenv("ENTRY_PRICE_DRIFT_FAVORABLE_MAX_PCT", "0.2")
    assert guard("BUY") is not None
    monkeypatch.setenv("ENTRY_PRICE_DRIFT_FAVORABLE_MAX_PCT", "0")
    board(900.0)
    assert guard("BUY") is None


def test_disabled(board, monkeypatch):
    monkeypatch.setenv("ENTRY_PRICE_DRIFT_MAX_PCT", "0")
    board(1100.0)
    assert guard("BUY") is None
