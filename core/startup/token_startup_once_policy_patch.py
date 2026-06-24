# -*- coding: utf-8 -*-
"""
Token startup-once policy patch.

Operational policy requested by the operator:

* Obtain the kabu Station token once during startup and store it in settings.ini.
* After startup, all API calls must read/use the token from settings.ini/runtime cache.
* Do not refresh the token automatically on 401 / APIキー不一致.

If the token becomes invalid during the day, restart the processes so the startup
bootstrap obtains a new token once.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-TOKEN-STARTUP-ONCE-SETTINGS-INI-NO-RUNTIME-REFRESH"
_INSTALLED = False
_ORIG_REFRESH = None
_ORIG_REQUEST = None


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


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

        # get_valid_token() reads token_manager.API_TOKEN first, then settings.ini.
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
    for key in ("KABU_API_TOKEN", "KABUSAPI_TOKEN", "AUKABU_TOKEN", "API_TOKEN", "TOKEN", "KABU_API_KEY", "KABUSAPI_API_KEY"):
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
        "KABUSAPI_TOKEN",
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

    def refresh_token_startup_once(apipassword=None, *args, **kwargs):
        # Explicit manual override, for emergency use only.
        if _env_bool("KABU_TOKEN_ALLOW_RUNTIME_REFRESH", False) or _env_bool("KABU_TOKEN_FORCE_REFRESH", False):
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

        # If settings.ini has no token yet, allow a single bootstrap acquisition.
        token = original(apipassword=apipassword) if apipassword is not None else original()
        try:
            _publish_env_token(str(token or ""))
        except Exception:
            pass
        logger.warning("[TOKEN STARTUP ONCE] bootstrap token acquired because settings.ini token was empty token_len=%d", len(str(token or "")))
        return token

    try:
        refresh_token_startup_once._original = original  # type: ignore[attr-defined]
        refresh_token_startup_once._token_startup_once_policy = True  # type: ignore[attr-defined]
        token_manager.refresh_token = refresh_token_startup_once  # type: ignore[attr-defined]
        token_manager._TOKEN_STARTUP_ONCE_POLICY_PATCHED = True  # type: ignore[attr-defined]
        token = _read_settings_token()
        if token:
            _publish_env_token(token)
        logger.warning("[TOKEN STARTUP ONCE] token_manager patched no_runtime_refresh=True token_len=%d", len(token))
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

        # token_manager may already have installed a 401-refresh wrapper.  Use the
        # stored original if available so this patch truly disables refresh/retry.
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

    # Make retry patches skip refresh by default.  They may still retry the same
    # settings.ini token if their own code does so, but they will not issue /token.
    os.environ.setdefault("PUSH_REGISTER_AUTH_RETRY_ENABLED", "0")
    os.environ.setdefault("KABU_TOKEN_ALLOW_RUNTIME_REFRESH", "0")
    os.environ.setdefault("KABU_TOKEN_FORCE_REFRESH", "0")

    ok_tm = _patch_token_manager()
    ok_req = _patch_requests_no_401_refresh()
    _INSTALLED = bool(ok_tm or ok_req)
    logger.warning(
        "[TOKEN STARTUP ONCE] installed version=%s token_manager=%s requests=%s auth_retry=%s",
        VERSION,
        ok_tm,
        ok_req,
        os.environ.get("PUSH_REGISTER_AUTH_RETRY_ENABLED"),
    )
    return _INSTALLED


__all__ = ["VERSION", "install"]
