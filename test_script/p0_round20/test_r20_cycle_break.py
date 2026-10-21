# -*- coding: utf-8 -*-
"""2026-09-01 の循環障害 (API逼迫 -> 登録失敗 -> PUSH停止 -> データ空) の回帰テスト。

実 API・実 WebSocket・実 Discord は一切呼ばない。すべてモック。
"""
from __future__ import annotations

import logging

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _reset_shared_singletons():
    """モジュールレベルの共有状態を毎回戻す。

    api_circuit / rest_coordinator / registration_state はプロセス内
    シングルトンなので、リセットしないと後続の別テストへ状態が漏れる
    (実際に test_ranking_turnover_units が breaker open を引き継いで落ちた)。
    """
    def _reset():
        for mod, fn in (
            ("trading.ranking.api_circuit", "reset_for_test"),
            ("kabu_api.rest_coordinator", "reset_for_test"),
            ("trading.push.registration_state", "reset_for_test"),
        ):
            try:
                import importlib

                getattr(importlib.import_module(mod), fn)()
            except Exception:
                pass

    _reset()
    yield
    _reset()


# ============================================================
# 1. ranking API: 429 は障害扱いにしない
# ============================================================

def test_rate_limit_does_not_open_breaker():
    from trading.ranking import api_circuit

    api_circuit.reset_for_test()
    for _ in range(5):
        api_circuit.note_rate_limited(0.0, "429")
    st = api_circuit.stats()
    assert st["open_streak"] == 0, "429 で指数バックオフが進んでいる: %s" % st
    assert st["opens"] == 0, "429 で breaker を開いている: %s" % st


def test_real_failure_still_opens_breaker():
    """429 を無害化しても、本物の失敗では従来どおり fail-fast する。"""
    from trading.ranking import api_circuit

    api_circuit.reset_for_test()
    api_circuit.begin_cycle()
    opened = False
    for _ in range(10):
        if api_circuit.note_failure("timeout"):
            opened = True
            break
    assert opened, "連続失敗で breaker が開かない"
    assert api_circuit.allow_call() is False, "打ち切り後も呼び出しを許可している"


def test_api_client_classifies_outcomes():
    """None を返した理由 (timeout / 429 / HTTP) を保持していること。"""
    from trading.ranking import api_client

    for outcome in (
        api_client.OUTCOME_TIMEOUT,
        api_client.OUTCOME_RATE_LIMITED,
        api_client.OUTCOME_HTTP_ERROR,
    ):
        api_client._set_outcome(outcome, "detail", 429)
        got, detail, status = api_client.last_outcome()
        assert got == outcome
        assert detail == "detail"
        assert status == 429


def test_api_client_uses_requests_not_urllib():
    """urllib は Connection: close で kabu Station を無応答にする。"""
    import inspect

    from trading.ranking import api_client

    src = inspect.getsource(api_client._request_once)
    assert "_session()" in src, "_request_once が requests セッションを使っていない"
    assert "urlopen" not in src, "_request_once がまだ urllib を使っている"


# ============================================================
# 2. 出来高フィルタ: 日次流動性の閾値を 1 本足に当てない
# ============================================================

def test_liquidity_prefers_cumulative_volume():
    from trading.ranking.filters import liquidity_filter as lf

    assert lf.VOLUME_COLUMNS[0] in lf.CUMULATIVE_VOLUME_COLUMNS

    df = pd.DataFrame({
        "symbol": ["A", "B"],
        "volume": [5500, 300],             # 3 分足 1 本ぶん
        "trading_volume": [2_000_000, 50_000],  # 当日累計
    })
    out = lf._volume_filter(df, 10000)
    assert out["symbol"].tolist() == ["A", "B"], "累計出来高があるのに 1 本足で落としている"


def test_liquidity_warns_when_only_per_bar_volume(caplog):
    from trading.ranking.filters import liquidity_filter as lf

    df = pd.DataFrame({"symbol": ["A", "B"], "volume": [5500, 300]})
    with caplog.at_level(logging.WARNING):
        out = lf._volume_filter(df, 10000)
    assert out.empty
    text = caplog.text
    assert "per-bar" in text or "全銘柄を除外" in text, "全滅の理由がログに残っていない"
    assert "vol_max" in text, "比較した出来高の分布が残っていない"


# ============================================================
# 3. 登録状態の一元管理
# ============================================================

def test_registration_state_separates_desired_and_confirmed():
    from trading.push import registration_state as rs

    rs.reset_for_test()
    rs.set_desired(["1301", "1332"], source="test")
    assert rs.registered_count() == 0, "desired で confirmed が動いている"

    rs.set_requested(["1301", "1332"], label="A")
    assert rs.registered_count() == 0, "requested で confirmed が動いている"

    rs.mark_registered_confirmed(["1301", "1332"], label="A", source="test")
    assert rs.confirmed_registered_symbols() == ["1301", "1332"]
    assert rs.last_good_registered_symbols() == ["1301", "1332"]


def test_registration_state_keeps_last_good_on_failure():
    from trading.push import registration_state as rs

    rs.reset_for_test()
    rs.mark_registered_confirmed(["1301", "1332"], label="A", source="test")
    rs.note_register_failed(label="B", reason="timeout")
    assert rs.registered_count() == 2, "register 失敗で confirmed が消えている"
    assert rs.last_good_registered_symbols() == ["1301", "1332"]


def test_registration_state_ignores_empty_confirm():
    from trading.push import registration_state as rs

    rs.reset_for_test()
    rs.mark_registered_confirmed(["1301"], label="A", source="test")
    rs.mark_registered_confirmed([], label="B", source="empty")
    assert rs.registered_count() == 1, "空で confirmed を上書きしている"


# ============================================================
# 4. 候補が空でも現状を壊さない / 固定ユニバース
# ============================================================

def test_refresh_sequence_refuses_empty_target(monkeypatch):
    """登録候補が空なら unregister を撃たない。"""
    from trading.push.subscription_manager import register_ops

    called = {"unregister_all": 0, "register": 0}
    monkeypatch.setattr(register_ops, "run_unregister_all",
                        lambda: called.__setitem__("unregister_all", called["unregister_all"] + 1) or True)
    monkeypatch.setattr(register_ops, "_register_once_with_content",
                        lambda syms, exchange=1: (called.__setitem__("register", called["register"] + 1), (True, {}))[1])

    ok = register_ops._run_refresh_sequence_core(["1301"] * 50, [], clear_first=True)
    assert ok is False
    assert called["unregister_all"] == 0, "候補が空なのに unregister/all を撃った"
    assert called["register"] == 0


def test_restore_last_good_after_failed_register(monkeypatch):
    """unregister 成功 -> register 失敗 のとき直前バッチへ戻す。"""
    from trading.push import registration_state as rs
    from trading.push.subscription_manager import register_ops

    rs.reset_for_test()
    good = ["%04d" % (1300 + i) for i in range(50)]
    rs.mark_registered_confirmed(good, label="A", source="test")

    target = ["%04d" % (2000 + i) for i in range(50)]
    attempts = []

    def fake_register(symbols, exchange=1):
        attempts.append(list(symbols))
        if list(symbols) == target:
            return False, {"Code": "9999", "Message": "boom"}
        return True, {"RegistList": [{"Symbol": s} for s in symbols]}

    monkeypatch.setattr(register_ops, "run_unregister_all", lambda: True)
    monkeypatch.setattr(register_ops, "_register_once_with_content", fake_register)
    monkeypatch.setattr(register_ops, "_try_differential_register", lambda *a, **k: None)

    ok = register_ops._run_refresh_sequence_core(good, target, clear_first=True)
    assert ok is False
    assert len(attempts) >= 2, "復旧登録が試みられていない: %s" % attempts
    assert attempts[-1] == good, "直前の正常バッチへ戻していない"
    assert rs.confirmed_registered_symbols() == good


def test_fallback_universe_builds_ab_with_fixed_slots():
    from trading.push import fallback_universe as fu

    pool = ["%04d" % (1000 + i) for i in range(200)]
    fixed = ["7203", "9984"]
    a, b = fu.build_ab_batches(pool, fixed=fixed, batch_size=50)
    assert len(a) == 50 and len(b) == 50
    assert a[:2] == fixed and b[:2] == fixed, "固定枠が A/B 双方に入っていない"
    assert set(a[2:]).isdisjoint(set(b[2:])), "可変枠が A/B で重複している"


def test_fallback_prefers_last_good(monkeypatch):
    from trading.push import fallback_universe as fu
    from trading.push import registration_state as rs

    rs.reset_for_test()
    good = ["1301", "1332", "1333"]
    rs.mark_registered_confirmed(good, label="A", source="test")

    monkeypatch.setattr(fu, "load_symbol_flags_universe", lambda limit=0: ["9999"] * 10)
    syms, source = fu.resolve_fallback_symbols(100)
    assert source == "last_good_registered"
    assert syms == good


# ============================================================
# 5. WebSocket 同一性
# ============================================================

def test_ws_snapshot_never_reports_connected_without_instance():
    from trading.push.push_stream import monitor as m
    from trading.push.push_stream import state as st

    st._ws_app = None
    st._connected_event.set()
    try:
        snap = m.ws_snapshot()
        assert snap["connected"] is False, "ws_app が無いのに connected=True"
        assert snap["instance_id"] == "none"
        assert snap["inconsistent"] is True
    finally:
        st._connected_event.clear()


def test_ws_snapshot_is_self_consistent():
    """instance_id / connected / callback_bound が同じ ws を指すこと。"""
    from trading.push.push_stream import monitor as m
    from trading.push.push_stream import state as st
    from trading.push.push_stream import ws_callbacks

    class FakeSock:
        connected = True

    class FakeWs:
        def __init__(self):
            self.sock = FakeSock()
            self.on_message = ws_callbacks.on_message

    ws = FakeWs()
    st._ws_app = ws
    st._connected_event.set()
    try:
        snap = m.ws_snapshot()
        assert snap["connected"] is True
        assert snap["ws_alive"] is True
        assert snap["callback_bound"] == "yes", snap
        assert ("%x" % id(ws)) in snap["instance_id"]
        assert snap["inconsistent"] is False
    finally:
        st._ws_app = None
        st._connected_event.clear()


def test_ws_generation_increases_on_reconnect():
    from trading.push.push_stream import ws_callbacks as wc

    g0 = wc.current_connection_generation()
    wc._conn_open(object())
    g1 = wc.current_connection_generation()
    wc._conn_open(object())
    g2 = wc.current_connection_generation()
    assert g1 == g0 + 1 and g2 == g1 + 1, "再接続で generation が進んでいない"


def test_stale_ws_callback_is_not_current():
    """古い WebSocket の callback が現行状態を書き換えないこと。"""
    from trading.push.push_stream import ws_callbacks as wc

    old_ws = object()
    wc._conn_open(old_ws)
    new_ws = object()
    wc._conn_open(new_ws)

    assert wc._is_current_ws(new_ws) is True
    assert wc._is_current_ws(old_ws) is False, "古い ws が current 扱いされている"


# ============================================================
# 6. 秘匿情報がログに出ない
# ============================================================

def test_webhook_url_is_masked():
    from utils import alerts_util

    url = "https://discord.com/api/webhooks/1335823633867739168/AbCdEf-secret_TOKEN"
    masked = alerts_util._mask_secrets(url)
    assert "AbCdEf-secret_TOKEN" not in masked, "Webhook トークンが露出している"
    assert "1335823633867739168" not in masked, "Webhook ID が露出している"


def test_rest_coordinator_priority_and_degrade():
    """高優先度が走っている間は低優先度が待つ / degraded で低優先度だけ止まる。"""
    import threading
    import time

    from kabu_api import rest_coordinator as rc

    rc.reset_for_test()

    started = threading.Event()
    release = threading.Event()

    def hold_order():
        with rc.acquire(rc.PRIO_ORDER, label="order"):
            started.set()
            release.wait(5.0)

    t = threading.Thread(target=hold_order, daemon=True)
    t.start()
    assert started.wait(2.0)

    t0 = time.monotonic()
    with pytest.raises(rc.RestBusy):
        with rc.acquire(rc.PRIO_RANKING, label="ranking", timeout=0.5):
            pass
    assert time.monotonic() - t0 >= 0.4, "低優先度が待たずに即失敗した"
    release.set()
    t.join(5.0)

    # degraded: RANKING は止まるが ORDER/REGISTER は通る
    rc.reset_for_test()
    rc.note_timeout(rc.PRIO_RANKING, "timeout")
    rc.note_timeout(rc.PRIO_RANKING, "timeout")
    assert rc.is_degraded() is True
    with pytest.raises(rc.RestBusy):
        with rc.acquire(rc.PRIO_RANKING):
            pass
    for prio in (rc.PRIO_ORDER, rc.PRIO_REGISTER):
        with rc.acquire(prio):
            pass  # 例外が出なければ OK


# ============================================================
# 7. TLS: 検証を落とさずに信頼チェーンを解決する
# ============================================================

def test_ca_bundle_merges_windows_store():
    """certifi だけで足りない環境向けに Windows ストアを足す。ネットワーク不要。"""
    import os

    from utils import ca_bundle

    path = ca_bundle.build_bundle(force=True)
    assert path and os.path.exists(path)
    data = open(path, "rb").read()
    assert data.count(b"BEGIN CERTIFICATE") > 100, "証明書がほとんど入っていない"


def test_discord_never_disables_tls_verification():
    from utils import alerts_util

    verify = alerts_util._resolve_ca_bundle()
    assert verify is not False, "verify=False になっている (中間者を検出できなくなる)"
    assert verify is None or verify is True or isinstance(verify, str)


def test_alerts_util_never_uses_verify_false():
    """AST 上で verify=False を実引数に渡している呼び出しが無いこと。

    コメントや docstring の説明文を誤検出しないよう、構文木で判定する。
    """
    import ast
    import inspect

    from utils import alerts_util

    tree = ast.parse(inspect.getsource(alerts_util))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords or []:
            if kw.arg != "verify":
                continue
            v = kw.value
            if isinstance(v, ast.Constant) and v.value is False:
                offenders.append(getattr(node.func, "attr", None) or getattr(node.func, "id", "?"))
    assert not offenders, "verify=False を実引数で渡している: %s" % offenders


# ============================================================
# 8. heavy rebuild が「やっていないのに done」と記録しない
# ============================================================

def test_heavy_rebuild_reports_not_applicable(monkeypatch, caplog):
    """tier2 が動かない文脈では done ではなく NOT APPLICABLE を出す。"""
    import logging
    import time as _time

    from scheduler_jobs.summary import runner_core as rc

    monkeypatch.setattr(rc, "_tier2_applicable_here", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(
        rc, "_job_summary_tier2_light_runner",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    rc._HEAVY_BG_RUNNING = False
    rc._HEAVY_BG_LAST_AT = 0.0

    with caplog.at_level(logging.WARNING):
        started = rc._submit_heavy_rebuild_bg(1, "now", False, False, {})
        for _ in range(50):
            if not rc._HEAVY_BG_RUNNING:
                break
            _time.sleep(0.02)

    assert started is True
    assert called["n"] == 0, "動かない文脈なのに tier2 を呼んでいる"
    text = caplog.text
    assert "NOT APPLICABLE" in text, "実行していないのに done と記録している: %s" % text[-300:]
    assert "heavy rebuild bg done" not in text


def test_heavy_rebuild_logs_row_count(monkeypatch, caplog):
    """実行した場合は必ず rows= を残す (0 行なら ERROR)。"""
    import logging
    import time as _time

    import pandas as pd

    from scheduler_jobs.summary import runner_core as rc

    monkeypatch.setattr(rc, "_tier2_applicable_here", lambda: True)
    monkeypatch.setattr(rc, "_job_summary_tier2_light_runner",
                        lambda *a, **k: pd.DataFrame())
    rc._HEAVY_BG_RUNNING = False
    rc._HEAVY_BG_LAST_AT = 0.0

    with caplog.at_level(logging.WARNING):
        rc._submit_heavy_rebuild_bg(1, "now", False, False, {})
        for _ in range(50):
            if not rc._HEAVY_BG_RUNNING:
                break
            _time.sleep(0.02)

    assert "rows=0" in caplog.text, "行数を残していない: %s" % caplog.text[-300:]


# ============================================================
# 9. 1分足 summary が DB writer で保存経路に到達する
# ============================================================

def test_db_writer_uses_sync_heavy_fallback(monkeypatch):
    """DB writer では同期の重い再構築 (=保存経路) を通ること。

    2026-09-01: このゲートがプロセスを問わず False だったため、
    interval=1 が _job_summary_original_heavy に到達せず
    stock_summary_1min が更新されなかった。
    """
    from scheduler_jobs.summary import runner_core as rc

    monkeypatch.delenv("SUMMARY_MAIN_SYNC_HEAVY_FALLBACK", raising=False)

    monkeypatch.setattr(rc, "_is_database_process", lambda: True)
    assert rc._sync_heavy_fallback_allowed() is True, "DB writer が保存経路へ行かない"

    monkeypatch.setattr(rc, "_is_database_process", lambda: False)
    assert rc._sync_heavy_fallback_allowed() is False, "main.py の tick を待たせている"


def test_sync_heavy_fallback_env_override_wins(monkeypatch):
    from scheduler_jobs.summary import runner_core as rc

    monkeypatch.setattr(rc, "_is_database_process", lambda: True)
    monkeypatch.setenv("SUMMARY_MAIN_SYNC_HEAVY_FALLBACK", "0")
    assert rc._sync_heavy_fallback_allowed() is False, "env の明示指定が効いていない"
    monkeypatch.setenv("SUMMARY_MAIN_SYNC_HEAVY_FALLBACK", "1")
    assert rc._sync_heavy_fallback_allowed() is True


def test_interval_1_reaches_heavy_path_in_db_writer(monkeypatch):
    """interval=1 が DB writer で _job_summary_original_heavy まで届くこと。"""
    import pandas as pd

    from scheduler_jobs.summary import runner_core as rc

    monkeypatch.delenv("SUMMARY_MAIN_SYNC_HEAVY_FALLBACK", raising=False)
    monkeypatch.setattr(rc, "_is_database_process", lambda: True)
    monkeypatch.setattr(rc, "_fresh_input_hopeless", lambda now: "")
    monkeypatch.setattr(rc, "_job_summary_tier1_direct_memory", lambda now, run_entry: pd.DataFrame())
    monkeypatch.setattr(rc, "_job_summary_tier2_light_runner", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(rc, "_stage", lambda: None)
    monkeypatch.setattr(rc, "_log_fast_path_decision", lambda *a, **k: None)

    reached = {"n": 0}

    def fake_heavy(interval, display=True, now=None, run_entry=True, **kwargs):
        reached["n"] += 1
        return pd.DataFrame({"symbol": ["1301"]})

    monkeypatch.setattr(rc, "_job_summary_original_heavy", fake_heavy)

    out = rc.job_summary(1, display=False, run_entry=False)
    assert reached["n"] == 1, "interval=1 が保存経路 (_job_summary_original_heavy) に到達していない"
    assert len(out) == 1
