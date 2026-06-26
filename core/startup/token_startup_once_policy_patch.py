# -*- coding: utf-8 -*-
"""
Token startup-once policy patch.

Operational policy requested by the operator:

* Parent main.py / main_database.py may obtain a kabu Station token at startup.
* Child/data-collector/helper processes must never call /token during bootstrap.
* After startup, all API calls must read/use the token from settings.ini/runtime cache.
* Do not refresh the token automatically on 401 / APIキー不一致.

This module used to call token_manager.refresh_token() at import/install time in every
process.  In data collector children that was dangerous because even a successful
/token call can invalidate the token being used by another process.  V3 makes the
bootstrap parent-only and keeps child logs unambiguous.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V3-TOKEN-STARTUP-PARENT-ONLY-NO-CHILD-BOOTSTRAP"
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
            "[TOKEN STARTUP ONCE] parent bootstrap disabled; using existing settings.ini token token_len=%d",
            len(token),
        )
        return token

    try:
        token = str(original_refresh() or "").strip()
        if token:
            _publish_env_token(token)
            os.environ["KABU_TOKEN_STARTUP_BOOTSTRAPPED"] = "1"
            logger.warning(
                "[TOKEN STARTUP ONCE] parent startup bootstrap token refreshed/saved token_len=%d",
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
        "[TOKEN STARTUP ONCE] fallback to existing settings.ini token after parent bootstrap failure token_len=%d",
        len(token),
    )
    return token


def _patch_token_manager() -> bool:
    global _ORIG_REFRESH
    try:
        import token_manager  # type: ignore
    except Exception:
        logger.exception("[TOKEN STARTUP ONCE] import token_manager failed")
        return False

    if getattr(token_manager, "_TOKEN_STARTUP_ONCE_POLICY_PATCHED", False):
        return True

    original = getattr(token_manager, "refresh_token", None)
    if not callable(original):
        logger.warning("[TOKEN STARTUP ONCE] token_manager.refresh_token missing")
        return False
    _ORIG_REFRESH = original

    startup_token = _startup_bootstrap_token(original)

    def refresh_token_startup_once(apipassword=None, *args, **kwargs):
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
            token = original(apipassword=apipassword) if apipassword is not None else original()
            try:
                _publish_env_token(str(token or ""))
            except Exception:
                pass
            return token

        token = _read_settings_token()
        if token:
            _publish_env_token(token)
            logger.warning(
                "[TOKEN STARTUP ONCE] runtime refresh suppressed; using settings.ini token token_len=%d",
                len(token),
            )
            return token

        logger.error(
            "[TOKEN STARTUP ONCE] runtime refresh suppressed but settings.ini token is empty; restart parent with KABU_TOKEN_REFRESH_ON_STARTUP=1 or fix settings.ini"
        )
        return ""

    try:
        refresh_token_startup_once._original = original  # type: ignore[attr-defined]
        refresh_token_startup_once._token_startup_once_policy = True  # type: ignore[attr-defined]
        token_manager.refresh_token = refresh_token_startup_once  # type: ignore[attr-defined]
        token_manager._TOKEN_STARTUP_ONCE_POLICY_PATCHED = True  # type: ignore[attr-defined]
        token = startup_token or _read_settings_token()
        if token:
            _publish_env_token(token)
        logger.warning("[TOKEN STARTUP ONCE] token_manager patched no_runtime_refresh=True parent_only=True token_len=%d", len(token))
        return True
    except Exception:
        logger.exception("[TOKEN STARTUP ONCE] token_manager patch failed")
        return False


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


def install() -> bool:
    global _INSTALLED
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

    ok_tm = _patch_token_manager()
    ok_req = _patch_requests_no_401_refresh()
    _INSTALLED = bool(ok_tm or ok_req)
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
    return _INSTALLED


__all__ = ["VERSION", "install"]
