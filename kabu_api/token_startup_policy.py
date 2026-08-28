# -*- coding: utf-8 -*-
"""
Token startup-once policy patch.

Operational policy requested by the operator:

* Parent main.py / main_database.py may obtain a kabu Station token at startup.
* Child/data-collector/helper processes must never call /token during bootstrap.
* After startup, all API calls must read/use the token from settings.ini/runtime cache.
* Do not refresh the token automatically on 401 / APIキー不一致 **at runtime**.
* Exception (V4): if the STARTUP preflight is rejected, the parent may issue one
  fresh /token.  That is `startup stale token recovery = ON` /
  `runtime arbitrary refresh = OFF`.  See kabu_api/token_bootstrap.py.

This module used to call token_manager.refresh_token() at import/install time in every
process.  In data collector children that was dangerous because even a successful
/token call can invalidate the token being used by another process.  V3 makes the
bootstrap parent-only and keeps child logs unambiguous.

V4 replaces the TTL-only startup validity check with a read-only preflight.  TTL
only proved that settings.ini was written recently; after a kabu Station restart
the server had already dropped the token, so a "young" file meant a guaranteed
401 with no recovery path.  Set KABU_TOKEN_STARTUP_PREFLIGHT_RECOVERY=0 to get
the old TTL-only behaviour back.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V4-TOKEN-STARTUP-PREFLIGHT-STALE-RECOVERY"
_INSTALLED = False
_ORIG_REFRESH = None
_ORIG_REQUEST = None

_CHILD_MARKERS = (
    "push_receiver_runner.py",
    "ranking_collector_runner.py",
    "summary_database_runner.py",
    "yahoo_complement_runner.py",
    "db_prepare_runner.py",
)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x) for x in getattr(sys, "argv", [])).lower()
    except Exception:
        return ""


def _is_child_process() -> bool:
    txt = _argv_text()
    if any(marker in txt for marker in _CHILD_MARKERS):
        return True
    for key in ("DATA_COLLECTOR_CHILD", "KABU_CHILD_PROCESS", "IS_CHILD_PROCESS"):
        if _env_bool(key, False):
            return True
    return False


def _is_parent_process() -> bool:
    txt = _argv_text()
    return "main_database.py" in txt or "main.py" in txt


def _is_kabu_api_url(url: Any) -> bool:
    try:
        s = str(url or "").lower()
        return ("localhost:18080/kabusapi" in s or "127.0.0.1:18080/kabusapi" in s) and not s.rstrip("/").endswith("/token")
    except Exception:
        return False


def _merge_api_key(headers: Any, token: str | None) -> dict:
    out: dict = {}
    try:
        if headers:
            out.update(dict(headers))
    except Exception:
        pass
    if token:
        out["X-API-KEY"] = str(token)
    return out


def _read_settings_token() -> str:
    try:
        import token_manager  # type: ignore

        fn = getattr(token_manager, "get_valid_token", None)
        if callable(fn):
            token = str(fn() or "").strip()
            if token:
                return token
        token = str(getattr(token_manager, "API_TOKEN", "") or "").strip()
        if token:
            return token
    except Exception:
        pass
    for key in ("KABU_API_TOKEN", "KABUSAPI_TOKEN", "AUKABU_TOKEN", "API_TOKEN", "TOKEN", "KABU_API_KEY", "KABUSAPI_API_KEY", "X_API_KEY", "KABU_TOKEN"):
        token = str(os.environ.get(key) or "").strip()
        if token:
            return token
    return ""


def _publish_env_token(token: str) -> None:
    token = str(token or "").strip()
    if not token:
        return
    for key in (
        "KABU_API_TOKEN",
        "KABUSAPI_TOKEN",
        "AUKABU_TOKEN",
        "API_TOKEN",
        "TOKEN",
        "KABU_API_KEY",
        "KABUSAPI_API_KEY",
        "KABU_TOKEN",
        "X_API_KEY",
    ):
        try:
            os.environ[key] = token
        except Exception:
            pass
    try:
        import token_manager  # type: ignore
        token_manager.API_TOKEN = token
    except Exception:
        pass


def _startup_bootstrap_token(original_refresh) -> str:
    """Bootstrap token only in parent processes.

    Children/helpers read settings.ini/runtime token only.  This function must not
    call original_refresh() outside parent main.py/main_database.py.
    """
    if _is_child_process():
        token = _read_settings_token()
        if token:
            _publish_env_token(token)
        logger.warning(
            "[TOKEN STARTUP ONCE] child bootstrap skipped; using settings.ini token token_len=%d argv=%s",
            len(token),
            _argv_text(),
        )
        return token

    if (not _is_parent_process()) and _env_bool("KABU_TOKEN_PARENT_ONLY_REFRESH", True):
        token = _read_settings_token()
        if token:
            _publish_env_token(token)
        logger.warning(
            "[TOKEN STARTUP ONCE] non-parent bootstrap skipped; using settings.ini token token_len=%d argv=%s",
            len(token),
            _argv_text(),
        )
        return token

    if not _env_bool("KABU_TOKEN_REFRESH_ON_STARTUP", True):
        token = _read_settings_token()
        if token:
            _publish_env_token(token)
        logger.warning(
            "[TOKEN STARTUP ONCE] TOKEN_SOURCE=settings_ini_cached TOKEN_ACTION=reused "
            "parent bootstrap disabled token_len=%d",
            len(token),
        )
        return token

    if _env_bool("KABU_TOKEN_STARTUP_PREFLIGHT_RECOVERY", True):
        token = _startup_bootstrap_with_preflight()
        if token:
            return token
        # Preflight-driven bootstrap could not produce a usable token.  Do NOT fall
        # back to the TTL path: that is exactly the loop that used to hand a stale
        # token to the rest of startup.  main_database.main() re-runs the same state
        # machine and aborts with the real reason.
        token = _read_settings_token()
        if token:
            _publish_env_token(token)
        return token

    return _startup_bootstrap_legacy_ttl(original_refresh)


def _startup_bootstrap_with_preflight() -> str:
    """Parent startup token decided by a read-only preflight, not by TTL.

    Delegates to kabu_api.token_bootstrap, which recovers a stale cached token
    with at most one POST /token per process.  This function never raises and
    never aborts the process: the authoritative gate is main_database.main().
    """
    try:
        from kabu_api import token_bootstrap as _tb
        import token_manager  # type: ignore
    except Exception:
        logger.exception("[TOKEN STARTUP ONCE] preflight bootstrap unavailable; falling back")
        return ""

    calls_before = token_manager.token_api_call_count()
    try:
        result = _tb.bootstrap_startup_token(label="startup_policy")
    except Exception:
        logger.exception("[TOKEN STARTUP ONCE] preflight bootstrap raised")
        return ""

    calls_made = token_manager.token_api_call_count() - calls_before
    if result.ok and result.token:
        _publish_env_token(result.token)
        os.environ["KABU_TOKEN_STARTUP_BOOTSTRAPPED"] = "1"
        os.environ["KABU_TOKEN_STARTUP_SOURCE"] = str(result.source)
        # Ground truth, not a guess: token_api_calls says whether /token was really
        # called.  The old log claimed "refreshed/saved" even when it reused a token.
        logger.warning(
            "[TOKEN STARTUP ONCE] TOKEN_SOURCE=%s TOKEN_ACTION=preflight_ok "
            "token_api_calls=%d token_len=%d token_tail=%s",
            result.source,
            calls_made,
            _tb.token_len(result.token),
            _tb.token_tail(result.token),
        )
        return result.token

    logger.error(
        "[TOKEN STARTUP ONCE] TOKEN_ACTION=preflight_ng startup bootstrap failed %s",
        result.summary(),
    )
    return ""


def _startup_bootstrap_legacy_ttl(original_refresh) -> str:
    """Pre-Ver34 TTL-only bootstrap, kept behind KABU_TOKEN_STARTUP_PREFLIGHT_RECOVERY=0."""
    try:
        import token_manager  # type: ignore

        calls_before = token_manager.token_api_call_count()
    except Exception:
        token_manager = None  # type: ignore
        calls_before = 0

    try:
        token = str(original_refresh() or "").strip()
        if token:
            _publish_env_token(token)
            os.environ["KABU_TOKEN_STARTUP_BOOTSTRAPPED"] = "1"
            calls_made = (
                token_manager.token_api_call_count() - calls_before if token_manager else -1
            )
            logger.warning(
                "[TOKEN STARTUP ONCE] TOKEN_SOURCE=%s TOKEN_ACTION=%s legacy ttl bootstrap "
                "token_api_calls=%d token_len=%d",
                getattr(token_manager, "LAST_TOKEN_SOURCE", "") or "unknown",
                getattr(token_manager, "LAST_TOKEN_ACTION", "") or "unknown",
                calls_made,
                len(token),
            )
            return token
        logger.error("[TOKEN STARTUP ONCE] parent startup bootstrap returned empty token")
    except Exception:
        logger.exception("[TOKEN STARTUP ONCE] parent startup bootstrap token refresh failed")

    token = _read_settings_token()
    if token:
        _publish_env_token(token)
    logger.warning(
        "[TOKEN STARTUP ONCE] TOKEN_SOURCE=settings_ini_cached TOKEN_ACTION=reused "
        "fallback after parent bootstrap failure token_len=%d",
        len(token),
    )
    return token


def refresh_token_startup_once(orig, apipassword=None, *args, **kwargs):
    """旧 refresh_token_startup_once (token_manager.refresh_token 差し替え)。

    起動時に 1 回だけ取得した settings.ini のトークンを使い、実行中の
    再取得は原則抑止する。子プロセスからの強制 refresh もブロックする。
    orig は素の token_manager.refresh_token。
    """
    if _env_bool("KABU_TOKEN_ALLOW_RUNTIME_REFRESH", False) or _env_bool("KABU_TOKEN_FORCE_REFRESH", False):
        if _is_child_process() and not _env_bool("KABU_TOKEN_ALLOW_CHILD_REFRESH", False):
            token = _read_settings_token()
            if token:
                _publish_env_token(token)
            logger.warning(
                "[TOKEN STARTUP ONCE] child forced refresh blocked; using settings.ini token token_len=%d argv=%s",
                len(token),
                _argv_text(),
            )
            return token
        token = orig(apipassword=apipassword) if apipassword is not None else orig()
        try:
            _publish_env_token(str(token or ""))
        except Exception:
            pass
        return token

    token = _read_settings_token()
    if token:
        _publish_env_token(token)
        logger.warning(
            "[TOKEN STARTUP ONCE] TOKEN_SOURCE=settings_ini_cached TOKEN_ACTION=reused "
            "runtime refresh suppressed (runtime arbitrary refresh = OFF) token_len=%d",
            len(token),
        )
        return token

    logger.error(
        "[TOKEN STARTUP ONCE] runtime refresh suppressed but settings.ini token is empty; restart parent with KABU_TOKEN_REFRESH_ON_STARTUP=1 or fix settings.ini"
    )
    return ""


def bootstrap_startup_token(orig) -> str:
    """旧 install() 内の _startup_bootstrap_token(original)。"""
    return _startup_bootstrap_token(orig)


def _patch_requests_no_401_refresh() -> bool:
    global _ORIG_REQUEST
    try:
        import requests  # type: ignore
    except Exception:
        return False

    try:
        current = requests.sessions.Session.request
        if getattr(current, "_token_startup_once_no_401_refresh", False):
            return True

        base = getattr(current, "_original", current)
        _ORIG_REQUEST = base

        def request_no_401_refresh(self, method, url, **kwargs):
            if _is_kabu_api_url(url):
                try:
                    token = _read_settings_token()
                    if token:
                        kwargs["headers"] = _merge_api_key(kwargs.get("headers"), token)
                except Exception:
                    pass
            resp = base(self, method, url, **kwargs)
            if _is_kabu_api_url(url) and getattr(resp, "status_code", None) == 401:
                logger.warning("[TOKEN STARTUP ONCE] 401 received; runtime refresh disabled url=%s", url)
            return resp

        request_no_401_refresh._original = base  # type: ignore[attr-defined]
        request_no_401_refresh._token_startup_once_no_401_refresh = True  # type: ignore[attr-defined]
        requests.sessions.Session.request = request_no_401_refresh
        logger.warning("[TOKEN STARTUP ONCE] requests patched no_401_refresh=True")
        return True
    except Exception:
        logger.exception("[TOKEN STARTUP ONCE] requests no-refresh patch failed")
        return False


_ENABLED = False


def startup_policy_enabled() -> bool:
    """旧 install() が走ったか。token_manager.refresh_token が見る。"""
    return bool(_ENABLED)


def enable_token_startup_policy() -> bool:
    """旧 install()。token_manager.refresh_token の差し替えは本文化済みなので、
    env 既定値・起動時ブートストラップ・requests の 401 再取得抑止だけを行う。"""
    global _INSTALLED, _ENABLED
    if _INSTALLED:
        return True
    if os.environ.get("DISABLE_TOKEN_STARTUP_ONCE_POLICY_PATCH", "").strip() == "1":
        logger.warning("[TOKEN STARTUP ONCE] disabled by env")
        return False

    os.environ.setdefault("PUSH_REGISTER_AUTH_RETRY_ENABLED", "0")
    os.environ.setdefault("KABU_TOKEN_ALLOW_RUNTIME_REFRESH", "0")
    os.environ.setdefault("KABU_TOKEN_FORCE_REFRESH", "0")
    os.environ.setdefault("KABU_TOKEN_REFRESH_ON_STARTUP", "1")
    os.environ.setdefault("KABU_TOKEN_PARENT_ONLY_REFRESH", "1")

    ok_tm = False
    try:
        import token_manager  # type: ignore

        if not getattr(token_manager, "_TOKEN_STARTUP_ONCE_POLICY_PATCHED", False):
            core = getattr(token_manager, "_refresh_token_core", None)
            startup_token = bootstrap_startup_token(core) if callable(core) else ""
            token_manager._TOKEN_STARTUP_ONCE_POLICY_PATCHED = True  # type: ignore[attr-defined]
            token = startup_token or _read_settings_token()
            if token:
                _publish_env_token(token)
            logger.warning("[TOKEN STARTUP ONCE] token policy active no_runtime_refresh=True parent_only=True token_len=%d", len(token))
        ok_tm = True
    except Exception:
        logger.exception("[TOKEN STARTUP ONCE] token policy bootstrap failed")

    ok_req = _patch_requests_no_401_refresh()
    _ENABLED = True
    _INSTALLED = True
    logger.warning(
        "[TOKEN STARTUP ONCE] installed version=%s token_manager=%s requests=%s auth_retry=%s startup_refresh=%s parent_only=%s child=%s",
        VERSION,
        ok_tm,
        ok_req,
        os.environ.get("PUSH_REGISTER_AUTH_RETRY_ENABLED"),
        os.environ.get("KABU_TOKEN_REFRESH_ON_STARTUP"),
        os.environ.get("KABU_TOKEN_PARENT_ONLY_REFRESH"),
        _is_child_process(),
    )
    return True


__all__ = ["VERSION", "enable_token_startup_policy", "startup_policy_enabled", "refresh_token_startup_once"]
