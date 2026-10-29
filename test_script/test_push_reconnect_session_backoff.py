# 2026-10-29: 場中の再接続の待ちを短くする / 自己切断・場外の切断を場中の「続いた回数」に数えない
#             / 10054 フィルタが自分の push_stream の警告を捨てない / 切断の理由を 1 行に出す
import datetime as dt
import logging

import pytest

from trading.push.push_stream import runner as RN
from trading.push.push_stream import state as ST
from trading.push.push_stream import ws_callbacks as WC

ENV_KEYS = (
    "PUSH_STREAM_RECONNECT_SESSION_AWARE",
    "PUSH_STREAM_RECONNECT_INTRADAY_LADDER_SEC",
    "PUSH_STREAM_RECONNECT_INTRADAY_MAX_SEC",
    "PUSH_STREAM_RECONNECT_RESET_ON_RECEIVE",
    "PUSH_STREAM_RECONNECT_BACKOFF_LADDER_SEC",
    "PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC",
    "PUSH_WS_10054_KEEP_LOGGER_PREFIXES",
    "PUSH_WS_SUPPRESS_EXPECTED_10054_TEXT",
)

THU = dt.date(2026, 10, 29)            # 木曜


def at(h, m, s=0):
    return dt.datetime.combine(THU, dt.time(h, m, s))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    RN.reset_reconnect_stats_for_test()
    ST._disconnect_info = {}
    yield
    RN.reset_reconnect_stats_for_test()
    ST._disconnect_info = {}


def note(uptime, now, *, received=0, self_close=None, base=0.3, max_wait=15.0, min_healthy=60.0):
    return RN._note_disconnect_and_wait(
        uptime, base=base, max_wait=max_wait, min_healthy=min_healthy,
        received=received, self_close_reason=self_close, now=now,
    )


# ---------------------------------------------------------------- 待ち時間

def test_intraday_fast_deaths_cap_at_2_seconds():
    waits = [note(1.0, at(10, 0, i))[0] for i in range(10)]
    assert waits[:5] == [0.3, 0.3, 0.5, 1.0, 2.0]
    assert max(waits) == 2.0 and waits[-1] == 2.0


def test_intraday_connection_with_data_resets_count():
    for i in range(6):
        note(1.0, at(10, 0, i))                              # 受信 0 の即死を 6 回
    assert RN.reconnect_stats()["consecutive_intraday"] == 6
    w, verdict = note(5.0, at(10, 1), received=120)          # 5 秒で RST だが受信はあった (10/29 の典型)
    assert verdict == "received_then_reset" and w == 0.3
    assert RN.reconnect_stats()["consecutive_intraday"] == 0


def test_reset_on_receive_can_be_disabled(monkeypatch):
    monkeypatch.setenv("PUSH_STREAM_RECONNECT_RESET_ON_RECEIVE", "0")
    for i in range(4):
        w, verdict = note(5.0, at(10, 0, i), received=100)
    assert verdict == "fast_death" and w == 1.0


def test_self_close_is_not_counted_intraday():
    note(1.0, at(10, 0, 0))
    note(1.0, at(10, 0, 1))
    before = RN.reconnect_stats()["consecutive_intraday"]
    for i in range(10):
        w, verdict = note(1.0, at(10, 0, 10 + i), self_close="last_recv_stale_while_ws_alive")
        assert verdict == "self_close"
    assert RN.reconnect_stats()["consecutive_intraday"] == before


def test_lunch_and_premarket_do_not_carry_into_session():
    # 寄り前: monitor の自己切断・即死を 50 回 -> 場外の回数だけが増え、待ちは従来のラダー (上限 15 秒)
    waits = [note(1.0, at(8, 0, i), self_close="startup_ws_never_connected")[0] for i in range(50)]
    assert max(waits) == 15.0
    st = RN.reconnect_stats()
    assert st["consecutive_off_session"] == 50 and st["consecutive_intraday"] == 0
    # 9:00 の最初の切断は単発扱い (場中の回数 1 -> 0.3 秒)
    w, verdict = note(1.0, at(9, 0, 5))
    assert w == 0.3 and verdict == "fast_death"


def test_session_open_resets_intraday_count_left_from_morning():
    for i in range(8):
        note(1.0, at(11, 29, i))                             # 前場の終わりに即死が 8 回
    assert RN.reconnect_stats()["consecutive_intraday"] == 8
    for i in range(20):
        note(1.0, at(12, 0, i), self_close="last_recv_stale_while_ws_alive")   # 昼休み
    w, _ = note(1.0, at(12, 30, 3))                          # 後場の最初の切断
    assert w == 0.3
    assert RN.reconnect_stats()["consecutive_intraday"] == 1


def test_off_session_wait_does_not_cross_the_open():
    for i in range(10):
        note(1.0, at(8, 59, 0))
    w, verdict = note(1.0, at(8, 59, 58))                    # 本来は 15 秒待ち
    assert w == pytest.approx(2.0) and verdict.endswith("_until_open")
    w, verdict = note(1.0, at(8, 59, 59, ) + dt.timedelta(microseconds=900000))
    assert w == 0.3                                          # base より短くはしない


def test_weekend_is_off_session():
    sat = dt.datetime(2026, 10, 31, 10, 0)
    assert RN._market_session(sat) == "off"
    assert RN._seconds_until_open(sat) is None
    assert RN._market_session(at(15, 29)) == "intraday"
    assert RN._market_session(at(15, 30)) == "off"
    assert RN._market_session(at(11, 30)) == "off"


def test_intraday_cap_is_configurable_for_rollback(monkeypatch):
    monkeypatch.setenv("PUSH_STREAM_RECONNECT_INTRADAY_LADDER_SEC", "1,2,4,8,15")
    monkeypatch.setenv("PUSH_STREAM_RECONNECT_INTRADAY_MAX_SEC", "15")
    waits = [note(1.0, at(10, 0, i))[0] for i in range(8)]
    assert max(waits) == 15.0


def test_session_aware_off_restores_old_behavior(monkeypatch):
    monkeypatch.setenv("PUSH_STREAM_RECONNECT_SESSION_AWARE", "0")
    # 旧実装: 場の時間・受信・自己切断を見ず、1 本の回数で 1,2,4,8,15 秒
    waits = [note(5.0, at(10, 0, i), received=100, self_close="x")[0] for i in range(7)]
    assert waits == [0.3, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0]
    w, verdict = note(70.0, at(10, 1))
    assert verdict == "healthy_disconnect" and w == 0.3


def test_defaults_match_the_code():
    from core.startup.runtime_env import defaults as D
    d = D.PUSH_DEFAULTS
    assert d["PUSH_STREAM_RECONNECT_SESSION_AWARE"] == "1"
    assert d["PUSH_STREAM_RECONNECT_INTRADAY_LADDER_SEC"] == "0.3,0.5,1,2"
    assert float(d["PUSH_STREAM_RECONNECT_INTRADAY_MAX_SEC"]) == 2.0
    assert d["PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC"] == "0.3"
    # 場外は従来の値のまま (8/28・9/2 の撃ち続けの対策を残す)
    assert d["PUSH_STREAM_RECONNECT_BACKOFF_LADDER_SEC"] == "1,2,4,8,15"
    assert float(d["PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC"]) == 15.0
    assert d["PUSH_REGISTER_DIFF_TRIM_EXTRA"] == "1"


# ---------------------------------------------------------------- 切断の理由

def test_reset_by_peer_is_recorded_and_formatted():
    ws = object()
    WC._conn_open(ws)
    err = ConnectionResetError(10054, "既存の接続はリモート ホストに強制的に切断されました。")
    WC.on_error(ws, err)
    WC.on_close(ws, None, None)
    info = RN._take_disconnect_info()
    text = RN._format_disconnect_cause(info)
    assert text.startswith("reset_by_peer(10054)")
    assert "close_code=None" in text and "error=" in text
    assert RN._take_disconnect_info() == {}                  # 取り出したら消える


def test_self_close_overrides_and_is_passed_to_backoff():
    from trading.push.push_stream import monitor as MO
    ws = object()
    WC._conn_open(ws)
    MO._force_close_ws_for_reconnect("last_recv_stale_while_ws_alive", stale_age=40.0, stale_count=2)
    WC.on_close(ws, 1000, "bye")
    info = RN._take_disconnect_info()
    assert info["self_close_reason"] == "last_recv_stale_while_ws_alive"
    assert RN._format_disconnect_cause(info).startswith("self_close:last_recv_stale_while_ws_alive")
    assert "close_code=1000" in RN._format_disconnect_cause(info)


def test_stale_socket_error_does_not_overwrite_cause():
    old, cur = object(), object()
    WC._conn_open(old)
    WC._conn_open(cur)
    WC.on_error(old, ConnectionResetError(10054, "old"))     # 古い socket の遅れた通知
    assert "cause" not in (ST._disconnect_info or {})


def test_unknown_cause_text():
    assert "unknown" in RN._format_disconnect_cause({})


# ---------------------------------------------------------------- run loop を通しで

class _StopAfter:
    """state._stop_event の代わり。wait() の秒数を記録し、n 回目で止める。"""

    def __init__(self, n):
        self.n = n
        self.waits = []
        self._set = False

    def is_set(self):
        return self._set

    def wait(self, sec):
        self.waits.append(sec)
        if len(self.waits) >= self.n:
            self._set = True
            return True
        return False

    def set(self):
        self._set = True

    def clear(self):
        self._set = False


def _fake_app_factory(script):
    """script(app) が run_forever の中身。WebSocket には一切つながない。"""

    class FakeApp:
        def __init__(self, url, on_open=None, on_message=None, on_error=None, on_close=None):
            self.url = url
            self.on_open, self.on_message, self.on_error, self.on_close = on_open, on_message, on_error, on_close
            self.sock = None

        def send(self, *_a, **_k):
            pass

        def close(self):
            pass

        def run_forever(self, *a, **k):
            script(self)

    return FakeApp


def _run_loop(monkeypatch, script, n, now):
    # 本番と同じ既定値 (sitecustomize が入れる PUSH_DEFAULTS) で流す
    from core.startup.runtime_env import defaults as D
    import os
    for k, v in D.PUSH_DEFAULTS.items():
        if k not in os.environ:
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", "1")
    monkeypatch.setenv("PUSH_WS_VENDOR_RUN_FOREVER", "1")
    monkeypatch.setenv("PUSH_WS_ENABLE_PING", "0")
    monkeypatch.setenv("PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER", "0")
    stop = _StopAfter(n)
    monkeypatch.setattr(ST, "_stop_event", stop)
    monkeypatch.setattr(RN, "_resolve_ws_url", lambda: "ws://127.0.0.1:9/websocket")
    monkeypatch.setattr(RN.websocket, "WebSocketApp", _fake_app_factory(script))
    monkeypatch.setattr(RN, "_jst_now", lambda: now)
    RN._run_forever_loop()
    return stop.waits


def test_run_loop_logs_reset_by_peer_and_reconnects_fast(monkeypatch, caplog):
    def script(app):
        app.on_open(app)
        ST._total_received += 25                         # 接続中に 25 件受けた
        app.on_error(app, ConnectionResetError(10054, "既存の接続はリモート ホストに強制的に切断されました。"))
        app.on_close(app, None, None)

    caplog.set_level(logging.WARNING, logger="trading.push.push_stream.runner")
    waits = _run_loop(monkeypatch, script, 6, at(10, 0))
    assert waits == [0.3] * 6                             # 受信のあった接続は回数を戻すので毎回 0.3 秒
    lines = [r.getMessage() for r in caplog.records if "reconnect after" in r.getMessage()]
    assert len(lines) == 6
    assert "cause=reset_by_peer(10054)" in lines[0]
    assert "recv_in_conn=25" in lines[0] and "session=intraday" in lines[0]
    assert "verdict=received_then_reset" in lines[0]
    assert not any("WS RECONNECT STORM" in r.getMessage() for r in caplog.records)


def test_run_loop_no_data_storm_is_capped_at_2s(monkeypatch):
    def script(app):
        app.on_open(app)
        app.on_error(app, ConnectionResetError(10054, "reset"))
        app.on_close(app, None, None)

    waits = _run_loop(monkeypatch, script, 12, at(10, 0))
    assert waits[:5] == [0.3, 0.3, 0.5, 1.0, 2.0]
    assert max(waits) == 2.0 and min(waits) >= 0.3       # 0 秒で撃ち続けない


def test_run_loop_lunch_self_close_then_afternoon(monkeypatch, caplog):
    from trading.push.push_stream import monitor as MO

    def script(app):
        app.on_open(app)
        MO._force_close_ws_for_reconnect("last_recv_stale_while_ws_alive", stale_age=36.0, stale_count=2)
        app.on_close(app, None, None)

    caplog.set_level(logging.WARNING, logger="trading.push.push_stream.runner")
    waits = _run_loop(monkeypatch, script, 8, at(12, 10))
    assert max(waits) == 15.0                             # 昼休みは従来のラダー
    line = [r.getMessage() for r in caplog.records if "reconnect after" in r.getMessage()][-1]
    assert "cause=self_close:last_recv_stale_while_ws_alive" in line and "session=off" in line
    assert not any("WS RECONNECT STORM" in r.getMessage() for r in caplog.records)
    # 後場の最初の切断は 0.3 秒
    w, _ = RN._note_disconnect_and_wait(1.0, base=0.3, max_wait=15.0, min_healthy=60.0, now=at(12, 30, 1))
    assert w == 0.3


# ---------------------------------------------------------------- 10054 フィルタ

def _rec(name, msg, level=logging.WARNING):
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def test_filter_keeps_own_reset_by_peer_warning():
    f = WC._Expected10054Filter()
    own = _rec("trading.push.push_stream.ws_callbacks",
               "[push_stream] ws reset by peer: [WinError 10054] 既存の接続はリモート ホストに強制的に切断されました。")
    assert f.filter(own) is True


def test_filter_still_drops_library_goodbye_noise():
    f = WC._Expected10054Filter()
    lib = _rec("websocket", "[WinError 10054] 既存の接続はリモート ホストに強制的に切断されました。 - goodbye",
               logging.ERROR)
    assert f.filter(lib) is False
    other = _rec("urllib3.connectionpool", "Connection reset by peer [WinError 10054] reset by peer")
    assert f.filter(other) is False                          # 自分以外の 10054 は従来どおり捨てる


def test_filter_old_behavior_with_empty_prefix(monkeypatch):
    monkeypatch.setenv("PUSH_WS_10054_KEEP_LOGGER_PREFIXES", "")
    f = WC._Expected10054Filter()
    own = _rec("trading.push.push_stream.ws_callbacks", "[push_stream] ws reset by peer: [WinError 10054] reset by peer")
    assert f.filter(own) is False


def test_filter_installed_on_root_handler_does_not_hide_own_warning(caplog):
    root = logging.getLogger()
    h = logging.Handler()
    seen = []
    h.emit = lambda r: seen.append(r.getMessage())
    root.addHandler(h)
    try:
        WC._install_expected_10054_filter()
        assert any(isinstance(x, WC._Expected10054Filter) for x in h.filters)
        ws = object()
        WC._conn_open(ws)
        WC.on_error(ws, ConnectionResetError(10054, "既存の接続はリモート ホストに強制的に切断されました。"))
    finally:
        root.removeHandler(h)
    assert any("ws reset by peer" in m for m in seen)
