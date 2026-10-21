# -*- coding: utf-8 -*-
"""
main_database.py startup gate tests.

Two things are pinned here:

1. _bootstrap_kabu_token_for_data_collectors() reports the right reason for each
   failure, and never returns True on an auth failure (no fail-open).
2. main() completes the token gate BEFORE starting the PUSH DB writer, the WAL
   checkpoint loop, or any child process.  The old order started the writer first
   and threw it away when auth failed.

No real kabu API call and no real child process is started; everything is stubbed.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import main_database  # noqa: E402
from kabu_api import token_bootstrap as tb  # noqa: E402

GOOD_TOKEN = "GOOD2222222222222222222222222ok1"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    tb.reset_startup_state()
    # _bootstrap_kabu_token_for_data_collectors() calls _ensure_basic_logging(),
    # which attaches a FileHandler under data_collectors.config.LOG_DIR -- the real
    # NAS log directory.  Without this, every test run drops a main_database_*.log
    # next to the production boot logs and the failure-path tests read like real
    # aborts.  caplog captures through the root logger either way.
    monkeypatch.setattr(main_database, "_ensure_basic_logging", lambda: None)
    yield
    tb.reset_startup_state()


def _result(**kwargs) -> tb.BootstrapResult:
    return tb.BootstrapResult(**kwargs)


def _patch_bootstrap(monkeypatch, result: tb.BootstrapResult):
    monkeypatch.setattr(tb, "bootstrap_startup_token", lambda **kw: result)


# ============================================================
# the gate itself
# ============================================================


def test_gate_passes_and_publishes_when_preflight_is_ok(monkeypatch):
    _patch_bootstrap(
        monkeypatch,
        _result(
            ok=True,
            token=GOOD_TOKEN,
            source=tb.SOURCE_SETTINGS_INI_CACHED,
            token_api_calls=0,
            preflight=tb.PreflightResult(ok=True, kind=tb.PF_OK, endpoint="/positions", status=200),
        ),
    )
    monkeypatch.delenv("KABU_TOKEN_PREFLIGHT_OK", raising=False)

    assert main_database._bootstrap_kabu_token_for_data_collectors() is True
    assert os.environ["KABU_TOKEN_PREFLIGHT_OK"] == "1"
    assert os.environ["KABU_TOKEN_PREFLIGHT_ENDPOINT"] == "/positions"


@pytest.mark.parametrize(
    "failure_kind",
    [
        tb.FAIL_API_PASSWORD_MISMATCH,
        tb.FAIL_FRESH_TOKEN_REJECTED,
        tb.FAIL_PREFLIGHT_TRANSPORT,
        tb.FAIL_TOKEN_API_TRANSPORT,
        tb.FAIL_TOKEN_API_ERROR,
        tb.FAIL_RECOVERY_ALREADY_USED,
    ],
)
def test_gate_never_fails_open(monkeypatch, failure_kind):
    _patch_bootstrap(monkeypatch, _result(ok=False, failure_kind=failure_kind, detail="x"))

    assert main_database._bootstrap_kabu_token_for_data_collectors() is False


def test_gate_logs_password_mismatch_distinctly(monkeypatch, caplog):
    caplog.set_level("ERROR")
    _patch_bootstrap(
        monkeypatch,
        _result(ok=False, failure_kind=tb.FAIL_API_PASSWORD_MISMATCH, token_api_calls=1, detail="401/4001009"),
    )

    assert main_database._bootstrap_kabu_token_for_data_collectors() is False

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TOKEN REQUEST FAILED" in text
    assert "NOT a stale cached token" in text
    assert "apipassword" in text


def test_gate_logs_stale_recovery_distinctly_from_password_mismatch(monkeypatch, caplog):
    caplog.set_level("ERROR")
    _patch_bootstrap(
        monkeypatch,
        _result(ok=False, failure_kind=tb.FAIL_FRESH_TOKEN_REJECTED, token_api_calls=1, detail="401"),
    )

    assert main_database._bootstrap_kabu_token_for_data_collectors() is False

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TOKEN REQUEST FAILED" not in text
    assert "freshly issued token was still rejected" in text
    assert "Not retrying" in text


def test_gate_logs_password_source_but_never_the_password(monkeypatch, caplog):
    caplog.set_level("WARNING")
    monkeypatch.setattr(main_database, "_read_api_password_from_settings", lambda: "s3cr3t-value")
    _patch_bootstrap(
        monkeypatch, _result(ok=True, token=GOOD_TOKEN, source=tb.SOURCE_SETTINGS_INI_CACHED)
    )

    main_database._bootstrap_kabu_token_for_data_collectors()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cr3t-value" not in text
    assert "api_password_source=settings.ini" in text
    assert "password_present=True" in text
    assert f"password_len={len('s3cr3t-value')}" in text


def test_gate_passes_the_settings_password_to_the_state_machine(monkeypatch):
    seen: dict = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return _result(ok=True, token=GOOD_TOKEN, source=tb.SOURCE_SETTINGS_INI_CACHED)

    monkeypatch.setattr(main_database, "_read_api_password_from_settings", lambda: "pw")
    monkeypatch.setattr(tb, "bootstrap_startup_token", capture)

    main_database._bootstrap_kabu_token_for_data_collectors()

    assert seen["apipassword"] == "pw"


def test_gate_survives_a_raising_state_machine(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(tb, "bootstrap_startup_token", boom)

    assert main_database._bootstrap_kabu_token_for_data_collectors() is False


# ============================================================
# startup ordering
# ============================================================


def _install_order_probes(monkeypatch, order: list, gate_result: bool):
    for name in (
        "_install_db_startup_scope_defaults",
        "_install_cpu_guard_env",
        "_install_summary_stale_guard",
        "_install_summary_wal_checkpoint",
        "_install_push_summary_realtime",
    ):
        monkeypatch.setattr(main_database, name, (lambda n: lambda: order.append(n))(name))

    def gate():
        order.append("token_gate")
        return gate_result

    monkeypatch.setattr(main_database, "_bootstrap_kabu_token_for_data_collectors", gate)
    monkeypatch.setattr(main_database, "_ensure_basic_logging", lambda: None)

    split_mode = types.ModuleType("data_collectors.split_mode")
    split_mode.mark_as_data_collector_process = lambda: order.append("mark_data_collector")
    monkeypatch.setitem(sys.modules, "data_collectors.split_mode", split_mode)

    runner = types.ModuleType("scripts.data_collectors_runner")
    runner.main = lambda: (order.append("children"), 0)[1]
    monkeypatch.setitem(sys.modules, "scripts.data_collectors_runner", runner)


def test_main_starts_no_worker_before_the_token_gate(monkeypatch):
    order: list = []
    _install_order_probes(monkeypatch, order, gate_result=True)

    assert main_database.main() == 0

    gate_at = order.index("token_gate")
    for heavy in ("_install_push_summary_realtime", "_install_summary_wal_checkpoint", "children"):
        assert order.index(heavy) > gate_at, f"{heavy} must start only after the token gate"


def test_main_aborts_before_any_worker_when_auth_fails(monkeypatch):
    order: list = []
    _install_order_probes(monkeypatch, order, gate_result=False)

    assert main_database.main() == 1

    assert "token_gate" in order
    assert "_install_push_summary_realtime" not in order, "PUSH DB writer must not start"
    assert "_install_summary_wal_checkpoint" not in order, "WAL checkpoint loop must not start"
    assert "children" not in order, "no child process on auth failure"


def test_main_still_sets_env_defaults_before_the_gate(monkeypatch):
    """Env-only setup is cheap and some of it is inherited by children; keep it first."""
    order: list = []
    _install_order_probes(monkeypatch, order, gate_result=True)

    main_database.main()

    gate_at = order.index("token_gate")
    for cheap in ("_install_db_startup_scope_defaults", "_install_cpu_guard_env", "_install_summary_stale_guard"):
        assert order.index(cheap) < gate_at


# ============================================================
# preflight shim still behaves
# ============================================================


def test_preflight_shim_returns_true_on_ok(monkeypatch):
    monkeypatch.setattr(
        tb, "preflight_token",
        lambda *a, **k: tb.PreflightResult(ok=True, kind=tb.PF_OK, endpoint="/positions", status=200),
    )
    monkeypatch.delenv("KABU_TOKEN_PREFLIGHT_OK", raising=False)

    assert main_database._preflight_kabu_token(GOOD_TOKEN) is True
    assert os.environ["KABU_TOKEN_PREFLIGHT_OK"] == "1"


def test_preflight_shim_returns_false_and_clears_env_on_auth_failure(monkeypatch):
    monkeypatch.setattr(
        tb, "preflight_token",
        lambda *a, **k: tb.PreflightResult(ok=False, kind=tb.PF_AUTH_REJECTED, endpoint="/positions", status=401),
    )
    os.environ["KABU_TOKEN_PREFLIGHT_OK"] = "1"

    assert main_database._preflight_kabu_token(GOOD_TOKEN) is False
    assert os.environ["KABU_TOKEN_PREFLIGHT_OK"] == "0"


def test_is_api_key_mismatch_shim_matches_shared_rule():
    assert main_database._is_api_key_mismatch({"Code": 4001009, "Message": "APIキー不一致"}) is True
    assert main_database._is_api_key_mismatch({"Code": 4001001}) is False
