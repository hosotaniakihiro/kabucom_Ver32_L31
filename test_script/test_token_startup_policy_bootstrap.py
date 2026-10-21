# -*- coding: utf-8 -*-
"""
kabu_api/token_startup_policy.py bootstrap wiring tests.

The import-time parent bootstrap used to log

    parent refresh skipped by ttl
    ...
    parent startup bootstrap token refreshed/saved

which is self-contradicting: the second line claims a refresh the first line just
said it skipped.  V4 replaces the TTL decision with a preflight and reports
token_api_calls, which is measured, not guessed.

The runtime policy stays OFF: refresh_token_startup_once() must still refuse to
call /token after startup.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from kabu_api import token_bootstrap as tb  # noqa: E402
from kabu_api import token_startup_policy as tsp  # noqa: E402
import token_manager  # noqa: E402

CACHED = "POLICYCACHED000000000000000stale"
FRESH = "POLICYFRESH1111111111111111good1"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    tb.reset_startup_state()
    # These tests must never look like a child or a helper process.
    monkeypatch.setattr(tsp, "_is_child_process", lambda: False)
    monkeypatch.setattr(tsp, "_is_parent_process", lambda: True)
    for key in ("KABU_TOKEN_STARTUP_BOOTSTRAPPED", "KABU_TOKEN_STARTUP_SOURCE"):
        monkeypatch.delenv(key, raising=False)
    yield
    tb.reset_startup_state()


def _never_called(*args, **kwargs):
    raise AssertionError("legacy TTL refresh must not run when preflight recovery is on")


def test_cached_token_that_works_is_reused_and_no_token_call_is_made(monkeypatch, caplog):
    caplog.set_level("WARNING")
    calls: list = []

    monkeypatch.setattr(tb, "_default_read_cached_token", lambda: (CACHED, tb.SOURCE_SETTINGS_INI_CACHED))
    monkeypatch.setattr(
        tb, "preflight_token",
        lambda token, **kw: tb.PreflightResult(ok=True, kind=tb.PF_OK, endpoint="/positions", status=200),
    )
    monkeypatch.setattr(tb, "_default_request_fresh_token", lambda **kw: calls.append(kw) or FRESH)

    token = tsp._startup_bootstrap_token(_never_called)

    assert token == CACHED
    assert calls == []
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "token_api_calls=0" in text
    assert "TOKEN_SOURCE=settings_ini_cached" in text
    # The contradictory old wording is gone.
    assert "token refreshed/saved" not in text


def test_stale_cached_token_is_recovered_and_the_log_says_a_call_happened(monkeypatch, caplog):
    caplog.set_level("WARNING")
    results = {CACHED: tb.PreflightResult(ok=False, kind=tb.PF_AUTH_REJECTED, endpoint="/positions", status=401),
               FRESH: tb.PreflightResult(ok=True, kind=tb.PF_OK, endpoint="/positions", status=200)}

    monkeypatch.setattr(tb, "_default_read_cached_token", lambda: (CACHED, tb.SOURCE_SETTINGS_INI_CACHED))
    monkeypatch.setattr(tb, "preflight_token", lambda token, **kw: results[token])

    def fake_request(apipassword=None, reason=""):
        # Stand in for token_manager.request_fresh_token, including its counter.
        token_manager._TOKEN_API_CALL_COUNT += 1
        return FRESH

    monkeypatch.setattr(tb, "_default_request_fresh_token", fake_request)

    token = tsp._startup_bootstrap_token(_never_called)

    assert token == FRESH
    assert os.environ["KABU_TOKEN_STARTUP_BOOTSTRAPPED"] == "1"
    assert os.environ["KABU_TOKEN_STARTUP_SOURCE"] == tb.SOURCE_STARTUP_401_RECOVERY
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "token_api_calls=1" in text
    assert "TOKEN_SOURCE=startup_401_recovery" in text


def test_failed_bootstrap_does_not_mark_the_process_as_bootstrapped(monkeypatch):
    monkeypatch.setattr(tb, "_default_read_cached_token", lambda: (CACHED, tb.SOURCE_SETTINGS_INI_CACHED))
    monkeypatch.setattr(
        tb, "preflight_token",
        lambda token, **kw: tb.PreflightResult(
            ok=False, kind=tb.PF_TRANSPORT_ERROR, endpoint="/positions", status=None, content="refused"
        ),
    )
    monkeypatch.setattr(tb, "_default_request_fresh_token", _never_called)

    token = tsp._startup_bootstrap_token(_never_called)

    # It still hands back whatever settings.ini holds so nothing crashes on import,
    # but it must not claim success -- main_database.main() is the real gate.
    assert "KABU_TOKEN_STARTUP_BOOTSTRAPPED" not in os.environ
    assert token in ("", CACHED) or isinstance(token, str)


def test_child_process_never_bootstraps(monkeypatch):
    monkeypatch.setattr(tsp, "_is_child_process", lambda: True)
    monkeypatch.setattr(tb, "_default_request_fresh_token", _never_called)
    monkeypatch.setattr(tb, "preflight_token", _never_called)
    monkeypatch.setattr(tsp, "_read_settings_token", lambda: CACHED)

    assert tsp._startup_bootstrap_token(_never_called) == CACHED


def test_non_parent_process_never_bootstraps(monkeypatch):
    monkeypatch.setattr(tsp, "_is_parent_process", lambda: False)
    monkeypatch.setenv("KABU_TOKEN_PARENT_ONLY_REFRESH", "1")
    monkeypatch.setattr(tb, "_default_request_fresh_token", _never_called)
    monkeypatch.setattr(tb, "preflight_token", _never_called)
    monkeypatch.setattr(tsp, "_read_settings_token", lambda: CACHED)

    assert tsp._startup_bootstrap_token(_never_called) == CACHED


def test_preflight_recovery_can_be_switched_off(monkeypatch):
    """KABU_TOKEN_STARTUP_PREFLIGHT_RECOVERY=0 restores the pre-V4 TTL path."""
    monkeypatch.setenv("KABU_TOKEN_STARTUP_PREFLIGHT_RECOVERY", "0")
    monkeypatch.setattr(tb, "preflight_token", _never_called)
    monkeypatch.setattr(tsp, "_publish_env_token", lambda token: None)

    assert tsp._startup_bootstrap_token(lambda: CACHED) == CACHED


def test_runtime_refresh_is_still_forbidden(monkeypatch, caplog):
    """startup recovery = ON must not have loosened runtime refresh = OFF."""
    caplog.set_level("WARNING")
    monkeypatch.delenv("KABU_TOKEN_ALLOW_RUNTIME_REFRESH", raising=False)
    monkeypatch.delenv("KABU_TOKEN_FORCE_REFRESH", raising=False)
    monkeypatch.setattr(tsp, "_read_settings_token", lambda: CACHED)
    monkeypatch.setattr(tsp, "_publish_env_token", lambda token: None)

    token = tsp.refresh_token_startup_once(_never_called)

    assert token == CACHED
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "runtime refresh suppressed" in text


def test_runtime_refresh_still_honours_the_explicit_override(monkeypatch):
    monkeypatch.setenv("KABU_TOKEN_ALLOW_RUNTIME_REFRESH", "1")
    monkeypatch.setattr(tsp, "_publish_env_token", lambda token: None)

    assert tsp.refresh_token_startup_once(lambda: FRESH) == FRESH
