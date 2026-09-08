# -*- coding: utf-8 -*-
"""ranking 復旧まわりの追加回帰テスト。

実 API・実 WebSocket・実 Discord・DB 書き込みは一切行わない。
"""
from __future__ import annotations

import datetime as dt
import os

import pytest


@pytest.fixture(autouse=True)
def _reset_singletons():
    def _reset():
        for mod, fn in (
            ("trading.ranking.api_circuit", "reset_for_test"),
            ("kabu_api.rest_coordinator", "reset_for_test"),
            ("trading.push.registration_state", "reset_for_test"),
            ("trading.push.fallback_universe", "reset_cache_for_test"),
        ):
            try:
                import importlib

                getattr(importlib.import_module(mod), fn)()
            except Exception:
                pass

    _reset()
    yield
    _reset()


class _Resp:
    """requests.Response の最小モック。"""

    def __init__(self, status: int, text: str = "{}", headers: dict | None = None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class _StubSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        r = self._responses.pop(0) if self._responses else _Resp(200, '{"Ranking": []}')
        if isinstance(r, Exception):
            raise r
        return r


# ============================================================
# 1. requests.Session の再利用
# ============================================================

def test_ranking_reuses_single_session():
    from trading.ranking import api_client

    api_client._SESSION = None
    s1 = api_client._session()
    s2 = api_client._session()
    assert s1 is s2, "呼び出しごとに Session を作り直している (keep-alive が効かない)"


# ============================================================
# 2. 429 -> Retry-After を尊重する
# ============================================================

def test_429_respects_retry_after(monkeypatch):
    from trading.ranking import api_client

    slept = []
    monkeypatch.setattr(api_client.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(api_client, "_get_startup_token", lambda: "T" * 32)
    stub = _StubSession([
        _Resp(429, "rate limited", {"Retry-After": "1.5"}),
        _Resp(200, '{"Ranking": [{"Symbol": "1301"}]}'),
    ])
    monkeypatch.setattr(api_client, "_session", lambda: stub)

    data = api_client.get_data_from_api({"type": 1, "ExchangeDivision": "ALL"}, retry_max=2)
    assert data and data.get("Ranking"), "429 の後に成功しても結果を返せていない"
    assert slept and abs(slept[0] - 1.5) < 0.01, "Retry-After を尊重していない: %s" % slept


def test_429_is_classified_as_rate_limited(monkeypatch):
    from trading.ranking import api_client

    monkeypatch.setattr(api_client.time, "sleep", lambda s: None)
    monkeypatch.setattr(api_client, "_get_startup_token", lambda: "T" * 32)
    stub = _StubSession([_Resp(429, "rate limited"), _Resp(429, "rate limited")])
    monkeypatch.setattr(api_client, "_session", lambda: stub)
    data = api_client.get_data_from_api({"type": 1, "ExchangeDivision": "ALL"}, retry_max=2)
    assert data is None
    outcome, _detail, status = api_client.last_outcome()
    assert outcome == api_client.OUTCOME_RATE_LIMITED, outcome
    assert status == 429


# ============================================================
# 3. 429 が続いても指数バックオフを進めない
# ============================================================

def test_repeated_429_never_escalates_backoff():
    from trading.ranking import api_circuit

    for _ in range(20):
        api_circuit.note_rate_limited(0.0, "429")
    st = api_circuit.stats()
    assert st["open_streak"] == 0, "429 で backoff が伸びている: %s" % st
    assert st["opens"] == 0
    assert st["retry_in_sec"] <= 60.0, "429 のクールダウンが長すぎる: %s" % st


# ============================================================
# 4. 429 の後に 200 が返れば breaker は正常復帰する
# ============================================================

def test_breaker_recovers_after_429_then_success():
    from trading.ranking import api_circuit

    api_circuit.note_rate_limited(0.0, "429")
    api_circuit.note_success()
    st = api_circuit.stats()
    assert st["open"] is False, "成功後も breaker が開いたまま: %s" % st
    assert st["open_streak"] == 0


# ============================================================
# 5. timeout の後に 200 が返れば probe が復帰する
# ============================================================

def test_breaker_recovers_after_timeout_then_probe_success():
    from trading.ranking import api_circuit

    api_circuit.begin_cycle()
    for _ in range(10):
        if api_circuit.note_failure("timeout"):
            break
    assert api_circuit.stats()["open"] is True, "timeout 連続で breaker が開かない"

    # probe 成功でクローズ
    api_circuit.note_success()
    st = api_circuit.stats()
    assert st["open"] is False, "probe 成功後も開いたまま: %s" % st
    assert api_circuit.begin_cycle().decision == "run", "次サイクルが通常収集に戻らない"


# ============================================================
# 6. ranking rows が DB writer へ渡せる形をしている
# ============================================================

def test_collected_rows_have_writer_required_keys(monkeypatch):
    from trading.ranking import api_circuit, collectors

    payload = {
        "Ranking": [
            {"No": 1, "Symbol": "1301", "SymbolName": "極洋", "CurrentPrice": 1000.0,
             "ChangeRatio": 0.05, "TradingVolume": 120000, "TradingValue": 1.2e8},
        ]
    }
    monkeypatch.setattr(collectors, "get_data_from_api", lambda *a, **k: payload)
    monkeypatch.setattr(collectors, "_ranking_call_plan", lambda: [(1, "値上がり率", "ALL")])
    monkeypatch.setattr(collectors, "_sleep_between_ranking_calls", lambda *a, **k: None)
    api_circuit.reset_for_test()

    class _Mgr:
        def __getattr__(self, name):
            return lambda *a, **k: None

    raw, _snapshot = collectors.collect_ranking_rows(dt.datetime(2026, 9, 1, 10, 0), _Mgr())
    assert raw, "raw rows が空"
    row = raw[0]
    for key in ("symbol", "rank_type", "rank_position", "current_price"):
        assert key in row, "DB writer が必要とする列が無い: %s (keys=%s)" % (key, sorted(row))


# ============================================================
# 7. 累計出来高列が無い場合の挙動
# ============================================================

def test_liquidity_falls_back_to_per_bar_and_reports():
    import logging

    import pandas as pd

    from trading.ranking.filters import liquidity_filter as lf

    df = pd.DataFrame({"symbol": ["A", "B"], "volume": [50_000, 300]})
    logger = logging.getLogger("trading.ranking.filters.liquidity_filter")
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    logger.addHandler(handler)
    try:
        out = lf._volume_filter(df, 10000)
    finally:
        logger.removeHandler(handler)

    # 閾値は変えないので、累計が無くても閾値を超える銘柄は残る
    assert out["symbol"].tolist() == ["A"]
    assert any("per-bar" in m for m in records), "per-bar 判定であることを記録していない"


# ============================================================
# 8. PUSH 候補が空でも現登録を維持する
# ============================================================

def test_push_seed_keeps_registration_when_all_providers_empty(monkeypatch):
    from data_collectors import push_runtime
    from trading.push import registration_state as rs

    rs.reset_for_test()
    rs.mark_registered_confirmed(["1301", "1332"], label="A", source="test")

    # すべてのプロバイダーを空にする
    monkeypatch.setattr(push_runtime, "_provider_call", lambda fn: None)
    monkeypatch.setattr(push_runtime, "_extract_symbols", lambda src: [])

    got = push_runtime.seed_push_symbols_once()
    # fallback が使えるなら非空、使えなくても既存 confirmed は壊さない
    assert rs.confirmed_registered_symbols() == ["1301", "1332"], "seed 失敗で confirmed が壊れた"
    assert isinstance(got, list)


# ============================================================
# 9. last_good が壊れていても安全に動く
# ============================================================

def test_corrupted_last_good_file_is_safe(tmp_path, monkeypatch):
    from trading.push import registration_state as rs

    bad = tmp_path / "state.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("PUSH_REGISTRATION_STATE_PATH", str(bad))

    rs.reset_for_test()
    rs._last_good_loaded = False  # ディスクを読ませる
    got = rs.last_good_registered_symbols()
    assert got == [], "壊れた last_good で例外/異常値になっている: %r" % (got,)


def test_last_good_write_failure_does_not_raise(monkeypatch):
    from trading.push import registration_state as rs

    rs.reset_for_test()
    monkeypatch.setattr(rs, "state_path", lambda: "Z:\\nonexistent\\dir\\state.json")
    # 書けなくても例外を投げないこと
    rs.mark_registered_confirmed(["1301"], label="A", source="test")
    assert rs.confirmed_registered_symbols() == ["1301"]


# ============================================================
# 10. CA バンドル生成に失敗しても検証を落とさない
# ============================================================

def test_ca_bundle_failure_falls_back_to_verification_on(monkeypatch):
    from utils import ca_bundle

    monkeypatch.setattr(ca_bundle, "build_bundle", lambda force=False: None)
    v = ca_bundle.verify_target()
    assert v is True, "バンドル生成失敗時に検証を無効化している: %r" % (v,)


def test_ca_bundle_install_respects_existing(monkeypatch, tmp_path):
    from utils import ca_bundle

    existing = tmp_path / "ca.pem"
    existing.write_text("x", encoding="utf-8")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(existing))
    monkeypatch.delenv("CA_BUNDLE_FORCE", raising=False)
    got = ca_bundle.install()
    assert got == str(existing), "既存の CA 設定を勝手に上書きしている"
