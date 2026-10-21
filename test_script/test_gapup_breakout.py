# ============================================================
# test_script/test_gapup_breakout.py
# ------------------------------------------------------------
# ギャップアップ＋初動5分足高値ブレイク戦略の state machine 単体テスト。
# 合成ティックのみで検証し、実 API / ランキング DB には触らない。
#   - 前日終値ルックアップは monkeypatch で固定
#   - 発注は LIVE=0（ドライラン）で検証し、fired 記録だけを確認する
# ============================================================

import os
import datetime as dt

import pytest

from trading.entry import gapup_breakout as gb


DAY = "2026-10-20"


def _tick(symbol, price, hhmmss, opening=None):
    return {
        "symbol": symbol,
        "current_price": price,
        "opening_price": opening,
        "current_price_time": f"{DAY}T{hhmmss}",
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # ドライラン固定・有効化
    monkeypatch.setenv("GAPUP_BREAKOUT_ENABLED", "1")
    monkeypatch.setenv("GAPUP_BREAKOUT_LIVE", "0")
    monkeypatch.setenv("GAPUP_BREAKOUT_GAP_PCT", "3.0")
    monkeypatch.setenv("GAPUP_BREAKOUT_WINDOW_START", "09:00:00")
    monkeypatch.setenv("GAPUP_BREAKOUT_WINDOW_LOCK", "09:05:00")
    monkeypatch.setenv("GAPUP_BREAKOUT_ENTRY_CUTOFF", "11:30:00")
    # fired JSON をテスト専用ディレクトリへ
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Discord 通知は無効化
    monkeypatch.setattr(gb, "_notify", lambda *a, **k: None)
    gb.reset_state()
    yield
    gb.reset_state()


def _set_prev_close(monkeypatch, value):
    monkeypatch.setattr(gb, "lookup_prev_close", lambda symbol: value)


def _feed(ticks):
    for t in ticks:
        gb.on_tick(t)


def test_gapup_breakout_fires_on_tick_break(monkeypatch):
    """寄り+4%ギャップ、初動5分高値=1050、9:05以降に1051で上抜け → 発火。"""
    _set_prev_close(monkeypatch, 1000.0)
    _feed([
        _tick("7777", 1040, "09:00:05", opening=1040),  # 寄り +4%
        _tick("7777", 1050, "09:02:00"),                # 初動5分足高値を 1050 に
        _tick("7777", 1045, "09:04:30"),
        _tick("7777", 1048, "09:05:10"),                # ロック直後、未ブレイク
        _tick("7777", 1051, "09:05:20"),                # 高値を上抜け → 発火
    ])
    st = gb.get_state("7777")
    assert st["gap_ok"] is True
    assert st["first5m_high"] == 1050
    assert st["locked"] is True
    assert st["fired"] is True


def test_no_fire_when_gap_below_threshold(monkeypatch):
    """寄り+2%はギャップ不成立 → GAP_NOT_MET でスキップ、上抜けても発火しない。"""
    _set_prev_close(monkeypatch, 1000.0)
    _feed([
        _tick("7777", 1020, "09:00:05", opening=1020),  # +2%
        _tick("7777", 1030, "09:02:00"),
        _tick("7777", 1099, "09:06:00"),                # 大きく上抜けても
    ])
    st = gb.get_state("7777")
    assert st["gap_ok"] is False
    assert st["skip"] is True
    assert st["skip_reason"] == "GAP_NOT_MET"
    assert st["fired"] is False


def test_no_fire_without_break(monkeypatch):
    """ギャップ成立でも初動5分高値を超えなければ発火しない。"""
    _set_prev_close(monkeypatch, 1000.0)
    _feed([
        _tick("7777", 1040, "09:00:05", opening=1040),
        _tick("7777", 1050, "09:03:00"),                # 高値 1050
        _tick("7777", 1049, "09:06:00"),                # 超えない
        _tick("7777", 1050, "09:07:00"),                # 同値（厳密上抜けでない）
    ])
    st = gb.get_state("7777")
    assert st["locked"] is True
    assert st["fired"] is False


def test_skip_when_no_prev_close(monkeypatch):
    """前日終値が取れない（ランキング未掲載）銘柄は対象外。"""
    _set_prev_close(monkeypatch, None)
    _feed([
        _tick("7777", 1040, "09:00:05", opening=1040),
        _tick("7777", 1050, "09:03:00"),
        _tick("7777", 1060, "09:06:00"),
    ])
    st = gb.get_state("7777")
    assert st["fired"] is False
    assert st["skip"] is True
    assert st["skip_reason"] == "NO_PREV_CLOSE_OR_OPEN"


def test_once_per_day_guard(monkeypatch):
    """発火は1日1回。発火後の更なる上抜けでは二重発火しない。"""
    _set_prev_close(monkeypatch, 1000.0)
    _feed([
        _tick("7777", 1040, "09:00:05", opening=1040),
        _tick("7777", 1050, "09:02:00"),
        _tick("7777", 1051, "09:05:20"),  # 1回目の発火
    ])
    assert gb.get_state("7777")["fired"] is True
    fire_calls = []
    monkeypatch.setattr(gb, "_fire", lambda *a, **k: fire_calls.append(a))
    gb.on_tick(_tick("7777", 1080, "09:06:00"))  # さらに上抜けても
    assert fire_calls == []  # _fire は呼ばれない（fired で早期 return）


def test_gap_exactly_3pct_fires(monkeypatch):
    """ちょうど +3.0%（境界）はギャップ成立（>=）。"""
    _set_prev_close(monkeypatch, 1000.0)
    _feed([
        _tick("7777", 1030, "09:00:05", opening=1030),  # ちょうど +3%
        _tick("7777", 1035, "09:03:00"),
        _tick("7777", 1036, "09:05:30"),
    ])
    st = gb.get_state("7777")
    assert st["gap_ok"] is True
    assert st["fired"] is True


def test_cutoff_blocks_entry(monkeypatch):
    """打ち切り時刻（既定11:30）以降はブレイクしても発火しない。"""
    _set_prev_close(monkeypatch, 1000.0)
    _feed([
        _tick("7777", 1040, "09:00:05", opening=1040),
        _tick("7777", 1050, "09:03:00"),
        _tick("7777", 1060, "11:35:00"),  # cutoff 超過
    ])
    st = gb.get_state("7777")
    assert st["fired"] is False
    assert st["skip"] is True
    assert st["skip_reason"] == "CUTOFF"


def test_premarket_ticks_ignored(monkeypatch):
    """寄り前（08:59）の tick は無視され、ステートも作られない。"""
    _set_prev_close(monkeypatch, 1000.0)
    gb.on_tick(_tick("7777", 1040, "08:59:30", opening=1040))
    assert gb.get_state("7777") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
