# -*- coding: utf-8 -*-
"""
Startup token state machine tests (kabu_api/token_bootstrap.py).

Every kabu call here is mocked.  Nothing in this file opens a socket, and no
order-placing endpoint is referenced at all -- the state machine is only ever
allowed to touch read-only endpoints (/positions, /wallet/cash).

Covers the six cases the recovery design has to get right:

CASE1 cached token valid            -> no /token call, startup OK
CASE2 cached token stale            -> preflight 401 -> /token once -> preflight 200 -> OK
CASE3 cached stale, /token 401      -> API password mismatch (NOT "stale token") -> abort
CASE4 no cached token               -> /token once -> preflight 200 -> OK
CASE5 fresh token still 401         -> abort without any retry loop
CASE6 kabu Station unreachable      -> fail-safe abort with a specific reason
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from kabu_api import token_bootstrap as tb  # noqa: E402
import token_manager  # noqa: E402


CACHED_TOKEN = "CACHED0000000000000000000000stale"
FRESH_TOKEN = "FRESH11111111111111111111111good"

OK_BODY = {"Positions": []}
MISMATCH_BODY = {"Code": 4001009, "Message": "APIキー不一致"}

READ_ONLY_ENDPOINTS = ("/positions", "/wallet/cash")


@pytest.fixture(autouse=True)
def _reset_state():
    tb.reset_startup_state()
    yield
    tb.reset_startup_state()


class TokenApiSpy:
    """Stands in for token_manager.request_fresh_token and counts real calls."""

    def __init__(self, token: str = FRESH_TOKEN, raises: Exception | None = None):
        self.token = token
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, apipassword=None, reason: str = ""):
        self.calls.append({"apipassword": apipassword, "reason": reason})
        if self.raises is not None:
            raise self.raises
        return self.token

    @property
    def count(self) -> int:
        return len(self.calls)


class PreflightSpy:
    """Maps token -> canned PreflightResult, and records every token asked about."""

    def __init__(self, results: dict):
        self.results = results
        self.seen: list[str] = []

    def __call__(self, token: str, label: str = ""):
        self.seen.append(token)
        result = self.results.get(token)
        if result is None:
            raise AssertionError(f"preflight called with an unexpected token tail={token[-4:]}")
        return result

    @property
    def count(self) -> int:
        return len(self.seen)


def _ok() -> tb.PreflightResult:
    return tb.PreflightResult(ok=True, kind=tb.PF_OK, endpoint="/positions", status=200, content=OK_BODY)


def _auth_rejected() -> tb.PreflightResult:
    return tb.PreflightResult(
        ok=False, kind=tb.PF_AUTH_REJECTED, endpoint="/wallet/cash", status=401, content=MISMATCH_BODY
    )


def _transport_error() -> tb.PreflightResult:
    return tb.PreflightResult(
        ok=False, kind=tb.PF_TRANSPORT_ERROR, endpoint="/wallet/cash", status=None,
        content="<urlopen error [WinError 10061] connection refused>",
    )


def _run(cached, preflight, request_fresh, **kwargs):
    return tb.bootstrap_startup_token(
        apipassword="dummy-password",
        read_cached_token=lambda: cached,
        request_fresh_token=request_fresh,
        preflight=preflight,
        label="test",
        **kwargs,
    )


# ============================================================
# CASE1: cached token valid -> no /token call
# ============================================================


def test_case1_cached_token_valid_makes_no_token_call():
    api = TokenApiSpy()
    pf = PreflightSpy({CACHED_TOKEN: _ok()})

    result = _run((CACHED_TOKEN, tb.SOURCE_SETTINGS_INI_CACHED), pf, api)

    assert result.ok is True
    assert result.token == CACHED_TOKEN
    assert result.source == tb.SOURCE_SETTINGS_INI_CACHED
    assert result.token_api_calls == 0
    assert api.count == 0, "a valid cached token must never trigger POST /token"
    assert pf.count == 1
    assert tb.startup_recovery_attempted() is False


def test_case1_stays_valid_even_when_settings_ini_is_old():
    """TTL is irrelevant now: an old file whose token still works is still used."""
    api = TokenApiSpy()
    pf = PreflightSpy({CACHED_TOKEN: _ok()})

    # No TTL/mtime input is consulted at all by the state machine.
    result = _run(CACHED_TOKEN, pf, api)

    assert result.ok is True
    assert result.token_api_calls == 0
    assert api.count == 0


# ============================================================
# CASE2: cached token stale -> one /token -> preflight OK
# ============================================================


def test_case2_stale_cached_token_recovers_with_exactly_one_token_call():
    api = TokenApiSpy(token=FRESH_TOKEN)
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected(), FRESH_TOKEN: _ok()})

    result = _run((CACHED_TOKEN, tb.SOURCE_SETTINGS_INI_CACHED), pf, api)

    assert result.ok is True
    assert result.token == FRESH_TOKEN
    assert result.source == tb.SOURCE_STARTUP_401_RECOVERY
    assert result.token_api_calls == 1
    assert api.count == 1, "startup recovery must call /token exactly once"
    assert pf.seen == [CACHED_TOKEN, FRESH_TOKEN], "one preflight before, one after"
    assert result.failure_kind == tb.FAIL_NONE
    assert result.is_password_mismatch is False


def test_case2_recovery_is_reported_as_startup_401_recovery_not_password_mismatch():
    """Case A must never be labelled case B."""
    api = TokenApiSpy(token=FRESH_TOKEN)
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected(), FRESH_TOKEN: _ok()})

    result = _run(CACHED_TOKEN, pf, api)

    assert result.source == tb.SOURCE_STARTUP_401_RECOVERY
    assert api.calls[0]["reason"] == tb.SOURCE_STARTUP_401_RECOVERY


def test_case2_second_bootstrap_in_same_process_does_not_call_token_again():
    """main_database.main() re-runs the machine after the import-time bootstrap."""
    api = TokenApiSpy(token=FRESH_TOKEN)
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected(), FRESH_TOKEN: _ok()})

    first = _run(CACHED_TOKEN, pf, api)
    assert first.ok is True and api.count == 1

    # Second pass sees the recovered token in settings.ini and just preflights it.
    second = _run(FRESH_TOKEN, pf, api)
    assert second.ok is True
    assert second.token_api_calls == 0
    assert api.count == 1, "total POST /token per process must stay at 1"


def test_case2_recovery_guard_blocks_a_second_recovery_in_the_same_process():
    api = TokenApiSpy(token=FRESH_TOKEN)
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected(), FRESH_TOKEN: _ok()})
    _run(CACHED_TOKEN, pf, api)
    assert api.count == 1

    # A later stale token cannot burn a second /token call.
    again = _run(CACHED_TOKEN, pf, api)
    assert again.ok is False
    assert again.failure_kind == tb.FAIL_RECOVERY_ALREADY_USED
    assert api.count == 1


# ============================================================
# CASE3: /token itself 401 -> API password mismatch
# ============================================================


def test_case3_token_api_401_is_password_mismatch_not_stale_token():
    api = TokenApiSpy(
        raises=token_manager.TokenApiPasswordMismatch(
            "POST /token rejected the APIPassword status=401 code=4001009", status=401, code="4001009"
        )
    )
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected()})

    result = _run(CACHED_TOKEN, pf, api)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_API_PASSWORD_MISMATCH
    assert result.is_password_mismatch is True
    assert result.token_api_calls == 1
    assert api.count == 1, "must not retry /token after a password mismatch"
    assert pf.count == 1, "no preflight is attempted when no token was issued"


def test_case3_missing_apipassword_is_also_a_password_mismatch():
    api = TokenApiSpy(
        raises=token_manager.TokenApiPasswordMismatch("settings.ini に apipassword がありません。")
    )
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected()})

    result = _run(CACHED_TOKEN, pf, api)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_API_PASSWORD_MISMATCH


# ============================================================
# CASE4: no cached token -> /token -> preflight OK
# ============================================================


def test_case4_no_cached_token_fetches_once_and_starts():
    api = TokenApiSpy(token=FRESH_TOKEN)
    pf = PreflightSpy({FRESH_TOKEN: _ok()})

    result = _run(("", ""), pf, api)

    assert result.ok is True
    assert result.token == FRESH_TOKEN
    assert result.source == tb.SOURCE_FRESH_TOKEN_API
    assert result.token_api_calls == 1
    assert api.count == 1
    assert pf.seen == [FRESH_TOKEN], "nothing to preflight before the token exists"


def test_case4_empty_token_from_api_is_an_error_not_a_success():
    api = TokenApiSpy(token="")
    pf = PreflightSpy({})

    result = _run("", pf, api)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_TOKEN_API_ERROR
    assert pf.count == 0


# ============================================================
# CASE5: fresh token still rejected -> abort, no retry loop
# ============================================================


def test_case5_fresh_token_still_401_aborts_without_retrying():
    api = TokenApiSpy(token=FRESH_TOKEN)
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected(), FRESH_TOKEN: _auth_rejected()})

    result = _run(CACHED_TOKEN, pf, api)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_FRESH_TOKEN_REJECTED
    assert result.is_password_mismatch is False, "the /token call itself succeeded"
    assert api.count == 1, "no infinite refresh: exactly one /token call"
    assert pf.count == 2, "exactly one preflight before and one after"


def test_case5_no_cached_token_and_fresh_token_rejected_also_aborts_once():
    api = TokenApiSpy(token=FRESH_TOKEN)
    pf = PreflightSpy({FRESH_TOKEN: _auth_rejected()})

    result = _run("", pf, api)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_FRESH_TOKEN_REJECTED
    assert api.count == 1
    assert pf.count == 1


# ============================================================
# CASE6: network / kabu Station down -> fail-safe
# ============================================================


def test_case6_preflight_transport_error_fails_safe_without_calling_token():
    api = TokenApiSpy()
    pf = PreflightSpy({CACHED_TOKEN: _transport_error()})

    result = _run(CACHED_TOKEN, pf, api)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_PREFLIGHT_TRANSPORT
    assert api.count == 0, "kabu Station is unreachable; do not burn a /token call"
    assert "connection refused" in result.detail
    assert tb.startup_recovery_attempted() is False, "the one-shot recovery is still available"


def test_case6_token_api_transport_error_is_reported_separately():
    api = TokenApiSpy(
        raises=token_manager.TokenApiTransportError(
            "POST /token could not reach kabu Station url=http://localhost:18080/kabusapi/token"
        )
    )
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected()})

    result = _run(CACHED_TOKEN, pf, api)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_TOKEN_API_TRANSPORT
    assert result.is_password_mismatch is False
    assert api.count == 1


def test_case6_unexpected_exception_from_token_api_does_not_fail_open():
    api = TokenApiSpy(raises=RuntimeError("something nobody predicted"))
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected()})

    result = _run(CACHED_TOKEN, pf, api)

    assert result.ok is False
    assert result.failure_kind == tb.FAIL_TOKEN_API_ERROR


def test_case6_broken_preflight_transport_never_looks_like_auth_failure():
    """A raising request_fn must be a transport error, not a 401."""

    def boom(url, token, timeout):
        raise OSError("socket exploded")

    result = tb.preflight_token(CACHED_TOKEN, request_fn=boom, label="test")

    assert result.ok is False
    assert result.kind == tb.PF_TRANSPORT_ERROR
    assert result.is_auth_rejected is False


# ============================================================
# preflight rules
# ============================================================


def test_preflight_only_touches_read_only_endpoints():
    seen: list[str] = []

    def record(url, token, timeout):
        seen.append(url)
        return False, 401, MISMATCH_BODY

    tb.preflight_token(CACHED_TOKEN, base_url="http://localhost:18080/kabusapi", request_fn=record)

    assert [u.rsplit("/kabusapi", 1)[1] for u in seen] == list(READ_ONLY_ENDPOINTS)
    for url in seen:
        assert "sendorder" not in url.lower()
        assert "cancelorder" not in url.lower()


def test_preflight_accepts_when_first_endpoint_401s_but_second_is_ok():
    def per_endpoint(url, token, timeout):
        if url.endswith("/positions"):
            return False, 401, MISMATCH_BODY
        return True, 200, {"StockAccountWallet": 100}

    result = tb.preflight_token(CACHED_TOKEN, request_fn=per_endpoint)

    assert result.ok is True
    assert result.endpoint == "/wallet/cash"


def test_preflight_treats_non_auth_http_error_as_token_accepted():
    """400/404 proves the request reached kabu Station and passed the API key check."""

    def not_found(url, token, timeout):
        return False, 404, {"Code": 4001001, "Message": "not found"}

    result = tb.preflight_token(CACHED_TOKEN, request_fn=not_found)

    assert result.ok is True
    assert result.kind == tb.PF_ACCEPTED_NON_AUTH


def test_preflight_detects_mismatch_body_even_on_a_200_shaped_failure():
    def mismatch_body(url, token, timeout):
        return False, 500, MISMATCH_BODY

    result = tb.preflight_token(CACHED_TOKEN, request_fn=mismatch_body)

    assert result.ok is False
    assert result.kind == tb.PF_AUTH_REJECTED


def test_preflight_rejects_empty_token_without_any_request():
    calls: list[str] = []

    def never(url, token, timeout):
        calls.append(url)
        return True, 200, {}

    result = tb.preflight_token("", request_fn=never)

    assert result.ok is False
    assert result.kind == tb.PF_EMPTY_TOKEN
    assert calls == []


def test_is_api_key_mismatch_matches_the_real_kabu_body():
    assert tb.is_api_key_mismatch({"Code": 4001009, "Message": "APIキー不一致"}) is True
    assert tb.is_api_key_mismatch('{"Code":4001009,"Message":"APIキー不一致"}') is True
    assert tb.is_api_key_mismatch({"Code": 4001001, "Message": "not found"}) is False
    assert tb.is_api_key_mismatch(None) is False


# ============================================================
# secrets must not reach the logs
# ============================================================


def test_logs_never_contain_the_token_value(caplog):
    caplog.set_level("DEBUG")
    api = TokenApiSpy(token=FRESH_TOKEN)
    pf = PreflightSpy({CACHED_TOKEN: _auth_rejected(), FRESH_TOKEN: _ok()})

    _run(CACHED_TOKEN, pf, api)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert CACHED_TOKEN not in text
    assert FRESH_TOKEN not in text
    assert "dummy-password" not in text
    # ...but the diagnostics that make triage possible are there.
    assert "token_len=" in text
    assert f"token_tail={FRESH_TOKEN[-4:]}" in text


def test_result_summary_never_leaks_the_token():
    result = tb.BootstrapResult(ok=True, token=FRESH_TOKEN, source=tb.SOURCE_FRESH_TOKEN_API)
    summary = result.summary()

    assert FRESH_TOKEN not in summary
    assert f"token_tail={FRESH_TOKEN[-4:]}" in summary
    assert f"token_len={len(FRESH_TOKEN)}" in summary


# ============================================================
# token_manager: /token failure classification (no sockets)
# ============================================================


def _http_error(status: int, body: str):
    import io as _io
    import urllib.error

    return urllib.error.HTTPError(
        url="http://localhost:18080/kabusapi/token",
        code=status,
        msg="err",
        hdrs=None,
        fp=_io.BytesIO(body.encode("utf-8")),
    )


def test_post_token_api_401_raises_password_mismatch(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(401, '{"Code":4001009,"Message":"APIキー不一致"}')

    monkeypatch.setattr(token_manager.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(token_manager.TokenApiPasswordMismatch) as exc:
        token_manager._post_token_api("whatever")

    assert exc.value.status == 401
    assert exc.value.code == "4001009"


def test_post_token_api_connection_refused_raises_transport_error(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(token_manager.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(token_manager.TokenApiTransportError):
        token_manager._post_token_api("whatever")


def test_post_token_api_counts_every_call(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(token_manager.urllib.request, "urlopen", fake_urlopen)

    before = token_manager.token_api_call_count()
    with pytest.raises(token_manager.TokenApiTransportError):
        token_manager._post_token_api("whatever")

    assert token_manager.token_api_call_count() == before + 1


def test_password_mismatch_is_a_token_api_error_subclass():
    """token_bootstrap catches Mismatch before Generic; ordering must stay correct."""
    assert issubclass(token_manager.TokenApiPasswordMismatch, token_manager.TokenApiError)
    assert issubclass(token_manager.TokenApiTransportError, token_manager.TokenApiError)


def test_apipassword_source_prefers_settings_ini_over_stale_runtime_cache(monkeypatch, tmp_path):
    from configparser import ConfigParser

    conf = ConfigParser()
    conf["aukabu"] = {"apipassword": "from-settings", "token": "t"}

    monkeypatch.setattr(token_manager, "API_PASSWORD", "stale-runtime-value")
    password, source = token_manager.resolve_apipassword_with_source(conf, "aukabu")

    assert password == "from-settings"
    assert source == "settings.ini[aukabu]"


def test_apipassword_argument_wins_and_source_is_reported():
    password, source = token_manager.resolve_apipassword_with_source(None, None, "explicit-value")

    assert password == "explicit-value"
    assert source == "argument"
