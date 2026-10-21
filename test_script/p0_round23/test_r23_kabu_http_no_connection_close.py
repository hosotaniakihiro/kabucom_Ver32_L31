# -*- coding: utf-8 -*-
"""2026-09-04: kabu Station 宛の残り urllib 経路を requests へ寄せた回帰テスト。

urllib.request は毎回 "Connection: close" を送り、kabu Station はそれに
応答を返さない (2026-08-29 実測。commit dddb0a7 で token 系だけ修正済み)。
その取りこぼしとして PUT /register・/positions・板読みが urllib のまま残り、
「kabu Station が実処理を伴うリクエストだけ無応答になる」という誤診断に
つながっていた。

実 API・実 WebSocket は一切呼ばない。すべてモック。
"""
from __future__ import annotations

import inspect
import io
import json
import urllib.error

import pytest


# ============================================================
# 1. kabu_http 本体
# ============================================================

def test_request_json_sends_no_connection_close_header():
    """requests に渡すヘッダーへ Connection を混ぜない (これだけが原因だった)。"""
    import requests

    from kabu_api import kabu_http

    seen = {}

    class _Res:
        status_code = 200
        reason = "OK"
        headers = {}
        text = json.dumps({"Code": 0})

    def _fake(method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        seen["headers"] = dict(kwargs.get("headers") or {})
        seen["timeout"] = kwargs.get("timeout")
        seen["data"] = kwargs.get("data")
        return _Res()

    orig = requests.request
    requests.request = _fake
    try:
        content = kabu_http.request_json(
            "http://localhost:18080/kabusapi/register",
            method="PUT",
            payload={"Symbols": [{"Symbol": "7203", "Exchange": 1}]},
            api_key="tok",
            timeout=7.5,
        )
    finally:
        requests.request = orig

    assert content == {"Code": 0}
    assert seen["method"] == "PUT"
    assert seen["timeout"] == 7.5
    assert seen["headers"]["X-API-KEY"] == "tok"
    assert seen["headers"]["Content-Type"] == "application/json"
    assert not any(k.lower() == "connection" for k in seen["headers"]), (
        "Connection ヘッダーを送ってはいけない。kabu Station が応答を返さなくなる"
    )
    assert json.loads(seen["data"].decode("utf-8"))["Symbols"][0]["Symbol"] == "7203"


def test_request_json_raises_httperror_compatible_with_urllib():
    """呼び出し側は .code と .read() を使う。型と使い勝手を変えない。"""
    import requests

    from kabu_api import kabu_http

    class _Res:
        status_code = 401
        reason = "Unauthorized"
        headers = {}
        text = json.dumps({"Code": "4001009", "Message": "APIキー不一致"}, ensure_ascii=False)

    orig = requests.request
    requests.request = lambda *a, **k: _Res()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            kabu_http.request_json("http://localhost:18080/kabusapi/positions", api_key="tok")
    finally:
        requests.request = orig

    assert ei.value.code == 401
    body = json.loads(ei.value.read().decode("utf-8"))
    assert body["Code"] == "4001009"


def test_request_json_returns_empty_string_for_empty_body():
    """本文が空のときに例外にしない (呼び出し側が [] へ畳む)。"""
    import requests

    from kabu_api import kabu_http

    class _Res:
        status_code = 200
        reason = "OK"
        headers = {}
        text = ""

    orig = requests.request
    requests.request = lambda *a, **k: _Res()
    try:
        assert kabu_http.request_json("http://localhost:18080/kabusapi/positions") == ""
    finally:
        requests.request = orig


# ============================================================
# 2. 呼び出し側に urllib が残っていないこと
# ============================================================

def test_register_ops_does_not_use_urlopen():
    from trading.push.subscription_manager import register_ops

    src = inspect.getsource(register_ops._http_json_request_impl)
    assert "urlopen" not in src, "PUT /register がまだ urllib を使っている"
    assert "kabu_http.request_json" in src


def test_position_reader_does_not_use_urlopen():
    from trading.position import kabu_position_reader

    # 関数名自体が _urlopen_positions なので、呼び出しの方を見る
    src = inspect.getsource(kabu_position_reader._urlopen_positions)
    assert "urllib.request.urlopen" not in src, "/positions がまだ urllib を使っている"
    assert "kabu_http.request_json" in src


def test_request_text_returns_status_and_body():
    """ステータスコードまで欲しい呼び出し (ATS) 用。本文はパースしない。"""
    import requests

    from kabu_api import kabu_http

    class _Res:
        status_code = 200
        reason = "OK"
        headers = {}
        text = '{"Result": 0}'

    orig = requests.request
    requests.request = lambda *a, **k: _Res()
    try:
        status, body = kabu_http.request_text(
            "http://localhost:18080/kabusapi/register", method="PUT", api_key="tok"
        )
    finally:
        requests.request = orig

    assert status == 200
    assert body == '{"Result": 0}', "request_text は本文を文字列のまま返す"


@pytest.mark.parametrize(
    "module_name",
    [
        "trading.push.subscription_manager.register_ops",
        "trading.position.kabu_position_reader",
        "trading.exit.executor",
        "trading.handlers.entry_order_builder",
        "kabu_api.board",
        "kabu_api.board_fetcher",
        "kabu_api.info",
        "ats.ats_api",
    ],
)
def test_kabu_callers_no_longer_import_urllib_request(module_name):
    """import が残っていると、いつのまにか元の書き方へ戻る。"""
    import importlib

    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    assert "urllib.request.urlopen" not in src, f"{module_name} に urlopen が残っている"
    assert "\nimport urllib.request" not in src, f"{module_name} が urllib.request を import している"


# ============================================================
# 3. /positions の 401/429 の扱いが変わっていないこと
# ============================================================

def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return urllib.error.HTTPError("http://localhost:18080/kabusapi/positions", code, "err", {}, io.BytesIO(raw))


def test_positions_429_does_not_refresh_token(monkeypatch):
    from trading.position import kabu_position_reader as r

    monkeypatch.setattr(r, "_resolve_base_and_token", lambda: ("http://localhost:18080/kabusapi", "tok"))
    monkeypatch.setattr(r, "_urlopen_positions", lambda *a, **k: (_ for _ in ()).throw(_http_error(429, {"Code": "4001010"})))

    called = {"refresh": 0}
    monkeypatch.setattr(r, "_refresh_token_after_401", lambda: called.__setitem__("refresh", called["refresh"] + 1))

    with pytest.raises(urllib.error.HTTPError):
        r._fetch_positions_payload_direct(product=2)
    assert called["refresh"] == 0, "429 でトークンを取り直してはいけない"


def test_positions_401_refreshes_token_once(monkeypatch):
    from trading.position import kabu_position_reader as r

    monkeypatch.setattr(r, "_resolve_base_and_token", lambda: ("http://localhost:18080/kabusapi", "old"))

    calls = []

    def _fake(base, token, *, product):
        calls.append(token)
        if token == "old":
            raise _http_error(401, {"Code": "4001009"})
        return [{"Symbol": "7203"}]

    monkeypatch.setattr(r, "_urlopen_positions", _fake)
    monkeypatch.setattr(r, "_refresh_token_after_401", lambda: "new")

    rows = r._fetch_positions_payload_direct(product=2)
    assert rows == [{"Symbol": "7203"}]
    assert calls == ["old", "new"]


def test_positions_empty_body_becomes_empty_list(monkeypatch):
    """空応答を「建玉ゼロ」として返すのは _urlopen_positions の責務のまま。"""
    import requests

    from kabu_api import kabu_http  # noqa: F401  (パッチ対象の確認用)
    from trading.position import kabu_position_reader as r

    class _Res:
        status_code = 200
        reason = "OK"
        headers = {}
        text = ""

    monkeypatch.setattr(r, "_positions_timeout_sec", lambda: 1.0)
    orig = requests.request
    requests.request = lambda *a, **k: _Res()
    try:
        assert r._urlopen_positions("http://localhost:18080/kabusapi", "tok", product=2) == []
    finally:
        requests.request = orig
