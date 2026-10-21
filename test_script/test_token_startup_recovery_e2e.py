# -*- coding: utf-8 -*-
"""
End-to-end startup token recovery over real sockets, against a stub kabu Station.

test_token_startup_recovery.py mocks the HTTP layer.  This file does not: it runs
a local HTTP server that behaves like kabu Station (issues tokens from /token,
answers 401 Code=4001009 on read-only endpoints when the X-API-KEY is not the one
it currently holds) and drives the real code path:

    preflight -> urllib GET
    recovery  -> token_manager.request_fresh_token -> urllib POST /token
              -> _save_token -> settings.ini written and re-read
              -> _publish_token -> env / module globals

The stub only implements read-only endpoints and /token.  There is no order
endpoint on it at all, so this test cannot place an order even by mistake.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from kabu_api import token_bootstrap as tb  # noqa: E402
import token_manager  # noqa: E402

API_PASSWORD = "stub-api-password"
READ_ONLY_PATHS = ("/kabusapi/positions", "/kabusapi/wallet/cash")

_PUBLISHED_ENV_KEYS = (
    "KABU_API_TOKEN", "KABUSAPI_TOKEN", "AUKABU_TOKEN", "API_TOKEN", "TOKEN",
    "KABU_API_KEY", "KABUSAPI_API_KEY", "X_API_KEY", "KABU_TOKEN",
    "KABU_STATION_TOKEN", "AUTOSTOCK_SETTINGS_INI_ACTIVE",
)


class StubKabuStation:
    """Minimal kabu Station: one live token at a time, like the real thing."""

    def __init__(self, api_password: str = API_PASSWORD):
        self.api_password = api_password
        self.live_token = "SERVERTOKEN00000000000000000001"
        self.token_calls = 0
        self.readonly_calls: list[tuple[str, str]] = []
        self.issue_seq = 1
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- server lifecycle -------------------------------------------------
    def start(self) -> str:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # keep pytest output clean
                pass

            def _send(self, status: int, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path != "/kabusapi/token":
                    self._send(404, {"Code": 4001001, "Message": "not found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    sent = json.loads(raw).get("APIPassword")
                except Exception:
                    sent = None
                stub.token_calls += 1
                if sent != stub.api_password:
                    self._send(401, {"Code": 4001009, "Message": "APIキー不一致"})
                    return
                stub.issue_seq += 1
                stub.live_token = f"SERVERTOKEN{stub.issue_seq:019d}"
                self._send(200, {"ResultCode": 0, "Token": stub.live_token})

            def do_GET(self):
                key = self.headers.get("X-API-KEY") or ""
                stub.readonly_calls.append((self.path, key))
                if self.path not in READ_ONLY_PATHS:
                    self._send(404, {"Code": 4001001, "Message": "not found"})
                    return
                if key != stub.live_token:
                    self._send(401, {"Code": 4001009, "Message": "APIキー不一致"})
                    return
                self._send(200, {"Positions": [], "StockAccountWallet": 0})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}/kabusapi"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- scenario helpers -------------------------------------------------
    def restart(self) -> None:
        """What a kabu Station restart does: every previously issued token dies."""
        self.live_token = "SERVERTOKEN_AFTER_RESTART_NOBODY_HAS"


@pytest.fixture
def stub():
    s = StubKabuStation()
    base = s.start()
    try:
        yield s, base
    finally:
        s.stop()


@pytest.fixture
def settings_ini(tmp_path, monkeypatch):
    path = tmp_path / "settings.ini"
    path.write_text(
        "[aukabu]\n"
        f"apipassword = {API_PASSWORD}\n"
        "token = SERVERTOKEN00000000000000000001\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOSTOCK_SETTINGS_INI", str(path))
    return path


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    tb.reset_startup_state()
    saved_env = {k: os.environ.get(k) for k in _PUBLISHED_ENV_KEYS}
    saved_token = token_manager.API_TOKEN
    saved_password = token_manager.API_PASSWORD
    saved_path = token_manager._CONFIG_FILE_PATH
    for k in _PUBLISHED_ENV_KEYS:
        os.environ.pop(k, None)
    token_manager.API_TOKEN = None
    token_manager.API_PASSWORD = None
    yield
    tb.reset_startup_state()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    token_manager.API_TOKEN = saved_token
    token_manager.API_PASSWORD = saved_password
    token_manager._CONFIG_FILE_PATH = saved_path


def _run(base: str, **kwargs):
    return tb.bootstrap_startup_token(
        preflight=lambda token, label: tb.preflight_token(token, base_url=base, label=label),
        label="e2e",
        **kwargs,
    )


def _ini_token(path) -> str:
    from configparser import ConfigParser

    conf = ConfigParser()
    conf.read(str(path), encoding="utf-8-sig")
    return conf.get("aukabu", "token", fallback="")


# ============================================================


def test_e2e_case1_valid_cached_token_makes_no_token_call(stub, settings_ini, monkeypatch):
    stub_server, base = stub
    monkeypatch.setattr(token_manager, "API_URL", base)
    before = token_manager.token_api_call_count()

    result = _run(base)

    assert result.ok is True
    assert result.token == stub_server.live_token
    assert result.source == tb.SOURCE_SETTINGS_INI_CACHED
    assert stub_server.token_calls == 0
    assert token_manager.token_api_call_count() == before
    assert _ini_token(settings_ini) == stub_server.live_token, "settings.ini untouched"


def test_e2e_case2_kabu_station_restart_is_recovered_with_one_token_call(stub, settings_ini, monkeypatch):
    """The exact production failure: cached token young, server side already dead."""
    stub_server, base = stub
    monkeypatch.setattr(token_manager, "API_URL", base)
    stale = _ini_token(settings_ini)
    stub_server.restart()

    result = _run(base)

    assert result.ok is True
    assert result.source == tb.SOURCE_STARTUP_401_RECOVERY
    assert result.token_api_calls == 1
    assert stub_server.token_calls == 1, "exactly one POST /token"
    assert result.token != stale
    assert result.token == stub_server.live_token
    # The new token really landed in settings.ini and in the runtime holders.
    assert _ini_token(settings_ini) == result.token
    assert token_manager.API_TOKEN == result.token
    assert os.environ["KABUSAPI_TOKEN"] == result.token
    assert os.environ["X_API_KEY"] == result.token
    # One preflight with the stale key, one with the fresh key.
    keys = [k for _p, k in stub_server.readonly_calls]
    assert stale in keys and result.token in keys


def test_e2e_case3_wrong_apipassword_is_reported_as_password_mismatch(stub, settings_ini, monkeypatch):
    stub_server, base = stub
    monkeypatch.setattr(token_manager, "API_URL", base)
    stub_server.restart()
    stub_server.api_password = "the-operator-changed-it"
    stale = _ini_token(settings_ini)

    result = _run(base)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_API_PASSWORD_MISMATCH
    assert result.is_password_mismatch is True
    assert stub_server.token_calls == 1, "no retry after a password mismatch"
    assert _ini_token(settings_ini) == stale, "a failed /token must not overwrite settings.ini"


def test_e2e_case4_no_cached_token_fetches_once(stub, tmp_path, monkeypatch):
    stub_server, base = stub
    path = tmp_path / "settings.ini"
    path.write_text(f"[aukabu]\napipassword = {API_PASSWORD}\ntoken = \n", encoding="utf-8")
    monkeypatch.setenv("AUTOSTOCK_SETTINGS_INI", str(path))
    monkeypatch.setattr(token_manager, "API_URL", base)

    result = _run(base)

    assert result.ok is True
    assert result.source == tb.SOURCE_FRESH_TOKEN_API
    assert stub_server.token_calls == 1
    assert _ini_token(path) == result.token


def test_e2e_case5_fresh_token_still_rejected_aborts_without_a_loop(stub, settings_ini, monkeypatch):
    """A server that hands out tokens it then refuses must not cause a refresh storm."""
    stub_server, base = stub
    monkeypatch.setattr(token_manager, "API_URL", base)

    stub_server.restart()

    # Hand back a token the stub will never accept, so both preflights fail while
    # /token itself succeeds -- the case that must NOT turn into a refresh storm.
    issued = {"n": 0}

    def bad_token(apipassword=None, reason=""):
        issued["n"] += 1
        stub_server.token_calls += 1
        return "ISSUED_BUT_NEVER_ACCEPTED_TOKEN"

    result = _run(base, request_fresh_token=bad_token)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_FRESH_TOKEN_REJECTED
    assert issued["n"] == 1, "exactly one /token call, no retry loop"
    assert stub_server.token_calls == 1


def test_e2e_case6_kabu_station_down_does_not_burn_a_token_call(stub, settings_ini, monkeypatch):
    stub_server, base = stub
    monkeypatch.setattr(token_manager, "API_URL", base)
    stub_server.stop()  # port closed from here on

    before = token_manager.token_api_call_count()
    result = _run(base)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_PREFLIGHT_TRANSPORT
    assert stub_server.token_calls == 0
    assert token_manager.token_api_call_count() == before
    assert tb.startup_recovery_attempted() is False, "recovery is still available on the next try"


def test_e2e_recovery_is_idempotent_across_two_bootstraps(stub, settings_ini, monkeypatch):
    """import-time bootstrap then main() bootstrap: still one /token in total."""
    stub_server, base = stub
    monkeypatch.setattr(token_manager, "API_URL", base)
    stub_server.restart()

    first = _run(base)
    second = _run(base)

    assert first.ok and second.ok
    assert second.token == first.token
    assert second.token_api_calls == 0
    assert stub_server.token_calls == 1, "the whole process issues one token, not two"


def test_e2e_stub_has_no_order_endpoint(stub, settings_ini, monkeypatch):
    """Guard the guard: this suite physically cannot place an order."""
    _stub_server, base = stub
    ok, status, _content = tb.http_get_json(f"{base}/sendorder", "any", 5.0)

    assert ok is False
    assert status == 404
